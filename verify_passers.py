"""verify_passers.py — Two-tier wallet verification for screen passers.

TIER 1 — Basic gate (fast, ~2 API calls per wallet):
  - Positive all-time profit (>= $500)
  - Surface stats: positions value, biggest win, prediction count

TIER 2 — Deep dive (slower, pulls ~3000 trades per wallet):
  - Win rate consistent across 3 time buckets (no single-month spike)
  - Market concentration (directional, not spray-and-pray)
  - Holding period (patient holder vs scalper)
  - Size consistency (not tilting or changing strategy)
  - Worst drawdown vs average win ratio
  - Still actively trading (recent activity)

Wallets that pass both tiers are PRIORITY SHADOW candidates.
Wallets that pass Tier 1 only are SHADOW candidates.

Thresholds configurable via pm-screen.yml under `tier2_filters`.

CLI:
  python scripts/verify_passers.py
  python scripts/verify_passers.py --passers /path/to/passers.json
  python scripts/verify_passers.py --addresses 0xABC,0xDEF
  python scripts/verify_passers.py --tier1-only   # skip deep dive
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data_sources.data_api import DataApiClient

OUT_DIR = Path(os.environ.get("PM_SCREEN_DIR", "/tmp/screen-v3"))
NOW = int(time.time())
DAY = 86_400

# ---------------------------------------------------------------------------
# Tier 2 defaults — override in pm-screen.yml under tier2_filters
# ---------------------------------------------------------------------------
T2_MIN_BUCKET_WR        = 0.55   # each 30d bucket win rate must be >= this
T2_MIN_CONCENTRATION    = 0.25   # unique_markets / total_trades >= this
T2_MAX_SIMULTANEOUS     = 15     # max avg open positions at any point
T2_MIN_HOLD_DAYS        = 1.0    # avg holding period >= N days (not a scalper)
T2_MAX_DRAWDOWN_RATIO   = 4.0    # worst_loss / avg_win must be < this
T2_MAX_SIZE_CV          = 2.0    # size coefficient of variation < this
T2_MIN_RECENT_TRADES    = 5      # must have >= N trades in last 30d


def load_tier2_config() -> dict:
    """Load tier2_filters from pm-screen.yml if present."""
    for candidate in [
        Path(__file__).resolve().parent / "pm-screen.yml",
        Path.cwd() / "pm-screen.yml",
        Path("/opt/polymarket-bot/scripts/pm-screen.yml"),
    ]:
        if candidate.exists():
            try:
                import yaml
                cfg = yaml.safe_load(candidate.read_text()) or {}
                return cfg.get("tier2_filters", {})
            except Exception:
                pass
    return {}


def fmt_usd(v) -> str:
    try:
        f = float(v or 0)
        if abs(f) >= 1_000_000:
            return f"${f/1_000_000:.1f}M"
        if abs(f) >= 1_000:
            return f"${f/1_000:.1f}K"
        return f"${f:.0f}"
    except (TypeError, ValueError):
        return "—"


def fmt_pnl(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
        sign = "+" if f >= 0 else ""
        if abs(f) >= 1_000_000:
            return f"{sign}${f/1_000_000:.1f}M"
        if abs(f) >= 1_000:
            return f"{sign}${f/1_000:.1f}K"
        return f"{sign}${f:.0f}"
    except (TypeError, ValueError):
        return "—"


# ---------------------------------------------------------------------------
# Tier 1 — surface profile stats
# ---------------------------------------------------------------------------

def get_profile_stats(api: DataApiClient, address: str) -> dict:
    """Fetch surface stats: all-time profit, positions value, biggest win,
    prediction count. Uses leaderboard API + positions endpoint + profile
    fallback."""
    stats = {
        "address": address,
        "positions_value": None,
        "all_time_profit": None,
        "biggest_win": None,
        "predictions": None,
        "error": None,
    }

    # Source 1: leaderboard portfolio API
    try:
        r = httpx.get(
            "https://lb-api.polymarket.com/portfolio",
            params={"address": address, "window": "all"},
            timeout=15,
        )
        if r.status_code == 200:
            lb = r.json()
            for key in ("profit", "pnl", "value", "totalProfit"):
                if lb.get(key) is not None:
                    stats["all_time_profit"] = float(lb[key])
                    break
            for key in ("tradesCount", "numTrades", "trades"):
                if lb.get(key) is not None:
                    stats["predictions"] = int(lb[key])
                    break
    except Exception:
        pass

    # Source 2: positions endpoint
    try:
        positions = api.get_positions_by_user(address, limit=500)
        if positions:
            pos_val = sum(float(p.get("currentValue") or 0) for p in positions)
            stats["positions_value"] = pos_val
            if stats["all_time_profit"] is None:
                stats["all_time_profit"] = sum(
                    float(p.get("cashPnl") or 0) for p in positions
                )
            if stats["predictions"] is None:
                stats["predictions"] = len(positions)
            wins = [float(p.get("cashPnl") or 0) for p in positions
                    if float(p.get("cashPnl") or 0) > 0]
            if wins:
                stats["biggest_win"] = max(wins)
    except Exception as exc:
        stats["error"] = f"positions: {str(exc)[:60]}"

    # Source 3: profile fallback
    if stats["predictions"] is None or stats["biggest_win"] is None:
        try:
            profile = api.get_profile(address)
            if profile:
                if stats["predictions"] is None:
                    for key in ("tradesCount", "trades_count", "numTrades"):
                        if profile.get(key) is not None:
                            stats["predictions"] = int(profile[key])
                            break
                if stats["biggest_win"] is None:
                    for key in ("biggestWin", "biggest_win"):
                        if profile.get(key) is not None:
                            stats["biggest_win"] = float(profile[key])
                            break
        except Exception:
            pass

    return stats


# ---------------------------------------------------------------------------
# Tier 2 — deep dive via paged activity
# ---------------------------------------------------------------------------

def _paged_activity(api: DataApiClient, addr: str, pages: int = 3) -> list[dict]:
    """Pull up to `pages` pages of TRADE + REDEEM activity."""
    acc, end = [], None
    for typ in ("TRADE", "REDEEM"):
        page_end = None
        for _ in range(pages):
            params = {"user": addr, "limit": 1000, "type": typ}
            if page_end is not None:
                params["end"] = page_end
            batch = api._get("/activity", params) or []
            if not batch:
                break
            acc.extend(batch)
            oldest = min(int(a.get("timestamp") or 0) for a in batch)
            if page_end is not None and oldest >= page_end:
                break
            page_end = oldest - 1
            if len(batch) < 1000:
                break
    return acc


def _usd(a: dict) -> float:
    v = a.get("usdcSize")
    if v is None:
        v = float(a.get("size") or 0) * float(a.get("price") or 0)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def deep_dive(api: DataApiClient, address: str, cfg: dict) -> dict:
    """Compute Tier 2 signals from paged activity history.

    Returns a dict with all computed signals and a `passes` bool.
    `reasons` lists which checks failed (empty = all passed).
    """
    min_bucket_wr    = cfg.get("min_bucket_wr",     T2_MIN_BUCKET_WR)
    min_concentration= cfg.get("min_concentration", T2_MIN_CONCENTRATION)
    max_simultaneous = cfg.get("max_simultaneous",  T2_MAX_SIMULTANEOUS)
    min_hold_days    = cfg.get("min_hold_days",     T2_MIN_HOLD_DAYS)
    max_dd_ratio     = cfg.get("max_drawdown_ratio",T2_MAX_DRAWDOWN_RATIO)
    max_size_cv      = cfg.get("max_size_cv",       T2_MAX_SIZE_CV)
    min_recent       = cfg.get("min_recent_trades", T2_MIN_RECENT_TRADES)

    result = {
        "bucket_win_rates": [],
        "concentration": None,
        "avg_hold_days": None,
        "worst_drawdown_ratio": None,
        "size_cv": None,
        "recent_trades_30d": None,
        "passes": False,
        "reasons": [],
        "error": None,
    }

    try:
        activity = _paged_activity(api, address, pages=3)
    except Exception as exc:
        result["error"] = str(exc)[:80]
        result["reasons"].append("fetch_failed")
        return result

    trades = [a for a in activity if str(a.get("type") or "").upper() == "TRADE"]
    redeems = [a for a in activity if str(a.get("type") or "").upper() == "REDEEM"]

    if len(trades) < 10:
        result["reasons"].append(f"insufficient_history ({len(trades)} trades)")
        return result

    # --- Signal 1: Win rate across 3 time buckets (90d, 60d, 30d) ----------
    cutoffs = [NOW - 90 * DAY, NOW - 60 * DAY, NOW - 30 * DAY, NOW]
    bucket_names = ["90–60d", "60–30d", "last 30d"]

    # Build per-market cashflows from all activity
    flow: dict[str, dict] = defaultdict(lambda: {"out": 0.0, "in": 0.0, "sh": 0.0,
                                                   "last_ts": 0})
    for a in trades:
        cid = a.get("conditionId")
        if not cid:
            continue
        side = str(a.get("side") or "").upper()
        sh = float(a.get("size") or 0)
        u = _usd(a)
        ts = int(a.get("timestamp") or 0)
        if side == "BUY":
            flow[cid]["out"] += u
            flow[cid]["sh"] += sh
            flow[cid]["last_ts"] = max(flow[cid]["last_ts"], ts)
        elif side == "SELL":
            flow[cid]["in"] += u
            flow[cid]["sh"] -= sh
            flow[cid]["last_ts"] = max(flow[cid]["last_ts"], ts)
    for a in redeems:
        cid = a.get("conditionId")
        if not cid:
            continue
        flow[cid]["in"] += _usd(a)
        flow[cid]["sh"] -= float(a.get("size") or 0)

    # Bucket closed markets by when they were last touched
    bucket_wins = [0, 0, 0]
    bucket_total = [0, 0, 0]
    for f in flow.values():
        if f["out"] <= 0:
            continue
        if abs(f["sh"]) > max(0.02 * f["out"], 1.0):
            continue  # still open
        ts = f["last_ts"]
        for bi in range(3):
            if cutoffs[bi] <= ts < cutoffs[bi + 1]:
                bucket_total[bi] += 1
                if f["in"] > f["out"]:
                    bucket_wins[bi] += 1

    bucket_wrs = []
    for bi in range(3):
        wr = (bucket_wins[bi] / bucket_total[bi]) if bucket_total[bi] >= 3 else None
        bucket_wrs.append(wr)
    result["bucket_win_rates"] = [
        {"bucket": bucket_names[bi], "wr": bucket_wrs[bi],
         "wins": bucket_wins[bi], "total": bucket_total[bi]}
        for bi in range(3)
    ]

    filled_buckets = [wr for wr in bucket_wrs if wr is not None]
    if len(filled_buckets) >= 2:
        if any(wr < min_bucket_wr for wr in filled_buckets):
            low = [f"{bucket_names[i]} {bucket_wrs[i]:.0%}"
                   for i in range(3) if bucket_wrs[i] is not None
                   and bucket_wrs[i] < min_bucket_wr]
            result["reasons"].append(f"wr_inconsistent ({', '.join(low)})")

    # --- Signal 2: Market concentration (unique markets / total trades) -----
    unique_markets = len({a.get("conditionId") for a in trades
                          if a.get("conditionId")})
    concentration = unique_markets / len(trades) if trades else 0
    result["concentration"] = round(concentration, 3)
    if concentration < min_concentration:
        result["reasons"].append(
            f"low_concentration ({concentration:.0%} unique mkts/trade)")

    # --- Signal 3: Average holding period ----------------------------------
    hold_days_list = []
    buy_ts: dict[str, int] = {}
    for a in sorted(trades, key=lambda x: int(x.get("timestamp") or 0)):
        cid = a.get("conditionId")
        if not cid:
            continue
        side = str(a.get("side") or "").upper()
        ts = int(a.get("timestamp") or 0)
        if side == "BUY":
            buy_ts[cid] = ts
        elif side == "SELL" and cid in buy_ts:
            hold = (ts - buy_ts[cid]) / DAY
            if hold >= 0:
                hold_days_list.append(hold)
            del buy_ts[cid]
    # also count redeems as exits
    for a in redeems:
        cid = a.get("conditionId")
        if cid and cid in buy_ts:
            hold = (int(a.get("timestamp") or 0) - buy_ts[cid]) / DAY
            if hold >= 0:
                hold_days_list.append(hold)

    avg_hold = statistics.mean(hold_days_list) if hold_days_list else None
    result["avg_hold_days"] = round(avg_hold, 1) if avg_hold is not None else None
    if avg_hold is not None and avg_hold < min_hold_days:
        result["reasons"].append(
            f"scalper (avg_hold={avg_hold:.1f}d < {min_hold_days}d)")

    # --- Signal 4: Worst drawdown ratio ------------------------------------
    trade_pnls = []
    for f in flow.values():
        if f["out"] > 0 and abs(f["sh"]) <= max(0.02 * f["out"], 1.0):
            trade_pnls.append(f["in"] - f["out"])

    if trade_pnls:
        wins_list = [p for p in trade_pnls if p > 0]
        losses_list = [p for p in trade_pnls if p < 0]
        avg_win = statistics.mean(wins_list) if wins_list else 1.0
        worst_loss = abs(min(losses_list)) if losses_list else 0.0
        dd_ratio = worst_loss / avg_win if avg_win > 0 else 0.0
        result["worst_drawdown_ratio"] = round(dd_ratio, 2)
        if dd_ratio >= max_dd_ratio:
            result["reasons"].append(
                f"high_drawdown (worst={fmt_usd(worst_loss)} "
                f"vs avg_win={fmt_usd(avg_win)}, ratio={dd_ratio:.1f}x)")

    # --- Signal 5: Trade size consistency (coefficient of variation) --------
    sizes = [_usd(a) for a in trades if _usd(a) > 0]
    if len(sizes) >= 5:
        mean_sz = statistics.mean(sizes)
        std_sz = statistics.stdev(sizes) if len(sizes) > 1 else 0
        cv = std_sz / mean_sz if mean_sz > 0 else 0
        result["size_cv"] = round(cv, 2)
        if cv >= max_size_cv:
            result["reasons"].append(
                f"erratic_sizing (cv={cv:.1f}, "
                f"mean={fmt_usd(mean_sz)}, std={fmt_usd(std_sz)})")

    # --- Signal 6: Recent activity -----------------------------------------
    recent = [a for a in trades
              if int(a.get("timestamp") or 0) >= NOW - 30 * DAY]
    result["recent_trades_30d"] = len(recent)
    if len(recent) < min_recent:
        result["reasons"].append(
            f"low_recent_activity ({len(recent)} trades in 30d)")

    result["passes"] = len(result["reasons"]) == 0
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Two-tier wallet verification for screen passers")
    parser.add_argument("--passers", default=str(OUT_DIR / "passers.json"),
                        help="Path to passers.json")
    parser.add_argument("--addresses", default=None,
                        help="Comma-separated addresses to check directly")
    parser.add_argument("--tier1-only", action="store_true",
                        help="Skip Tier 2 deep dive")
    args = parser.parse_args()

    t2_cfg = load_tier2_config()

    if args.addresses:
        addresses = [a.strip().lower() for a in args.addresses.split(",")]
        # Pull real screen metrics (win_rate, closed count) from
        # screened_wallets.json if available, instead of leaving screen_data
        # empty for every address. Without this, the Tier 1 win-rate
        # override (designed specifically to rescue wallets whose all-time
        # P&L is dragged negative by CURRENTLY-OPEN unrealized losses, even
        # though their REALIZED/closed track record is excellent) can never
        # fire when running via --addresses, since it has no win_rate/closed
        # data to evaluate. Confirmed this exact bug 2026-06-24: a wallet
        # with $69,199 closed_pnl and 90.2% win rate over 225 closed markets
        # was rejected as "NEGATIVE — SKIP" purely because its current open
        # positions happened to be underwater that day.
        screen_data = {a: {} for a in addresses}
        screened_path = OUT_DIR / "screened_wallets.json"
        if screened_path.exists():
            try:
                screened = json.loads(screened_path.read_text())
                for addr in addresses:
                    entry = screened.get(addr) or screened.get(addr.lower())
                    if entry:
                        m = entry.get("metrics", {})
                        screen_data[addr] = {
                            "win_rate": m.get("win_rate"),
                            "closed_markets": m.get("closed"),
                        }
            except Exception as exc:
                print(f"[warn] could not load screened_wallets.json for win-rate override data: {exc}")
    else:
        pf = Path(args.passers)
        if not pf.exists():
            print(f"[error] {pf} not found — run screen_directional.py first")
            sys.exit(1)
        passers = json.loads(pf.read_text())
        addresses = [p["address"] for p in passers]
        screen_data = {p["address"]: p for p in passers}

    if not addresses:
        print("No addresses to verify.")
        return

    print(f"\nVerifying {len(addresses)} wallets "
          f"({'Tier 1 only' if args.tier1_only else 'Tier 1 + 2'})...\n")
    print(f"{'#':<4} {'address':<16} {'pos_value':>10} {'all_time_pnl':>13} "
          f"{'biggest_win':>12} {'preds':>6}  {'screen_wr':>9}  tier1_status")
    print("-" * 100)

    results = []
    tier1_pass = []
    tier2_pass = []

    with DataApiClient() as api:
        for i, addr in enumerate(addresses, 1):
            # --- Tier 1 ---
            stats = get_profile_stats(api, addr)
            sd = screen_data.get(addr, {})
            atp = stats["all_time_profit"]

            if stats["error"] and atp is None:
                t1_status = f"ERROR: {stats['error']}"
                t1_ok = False
            elif atp is not None and atp < 0:
                # Win rate override: high WR + enough predictions passes despite negative cashPnl
                # (cashPnl distortion: unrealized losses drag all-time negative for active traders)
                wr = sd.get("win_rate") or 0
                closed = sd.get("closed_markets") or 0
                preds = stats.get("predictions") or 0
                # Trust win_rate if it's present and sample size (closed markets OR
                # predictions, whichever is available) is large enough
                sample_size = max(closed, preds)
                if (wr >= 0.60 and sample_size >= 50) or (wr == 0 and preds >= 100):
                    t1_status = "T1 PASS (wr override)"
                    t1_ok = True
                else:
                    t1_status = "NEGATIVE — SKIP"
                    t1_ok = False
            elif atp is not None and atp < 500:
                wr = sd.get("win_rate") or 0
                closed = sd.get("closed_markets") or 0
                preds = stats.get("predictions") or 0
                sample_size = max(closed, preds)
                if (wr >= 0.60 and sample_size >= 50) or (wr == 0 and preds >= 100):
                    t1_status = "T1 PASS (wr override)"
                    t1_ok = True
                else:
                    t1_status = "LOW ALL-TIME — SKIP"
                    t1_ok = False
            elif atp is None:
                t1_status = "no data — check manually"
                t1_ok = False
            else:
                t1_status = "T1 PASS"
                t1_ok = True

            wr = sd.get("win_rate")
            wr_str = f"{wr:.0%}" if wr is not None else "—"

            print(f"{i:<4} {addr[:14]}… {fmt_usd(stats['positions_value']):>10} "
                  f"{fmt_pnl(atp):>13} {fmt_usd(stats['biggest_win']):>12} "
                  f"{str(stats['predictions'] or '—'):>6}  "
                  f"{wr_str:>9}  {t1_status}")

            stats["screen_metrics"] = sd
            stats["tier1_pass"] = t1_ok
            stats["tier2"] = None
            results.append(stats)

            if t1_ok:
                tier1_pass.append(stats)

            time.sleep(0.3)

        # --- Tier 2 (only on Tier 1 survivors) ---
        if not args.tier1_only and tier1_pass:
            print(f"\n{'='*60}")
            print(f"TIER 2 DEEP DIVE — {len(tier1_pass)} wallet(s)\n")

            for stats in tier1_pass:
                addr = stats["address"]
                print(f"  {addr[:14]}… deep diving…", flush=True)

                t2 = deep_dive(api, addr, t2_cfg)
                stats["tier2"] = t2

                # Print Tier 2 signals
                bwrs = " | ".join(
                    f"{b['bucket']}: {b['wr']:.0%} ({b['wins']}/{b['total']})"
                    if b["wr"] is not None else f"{b['bucket']}: n/a"
                    for b in t2["bucket_win_rates"]
                )
                print(f"    win rate buckets : {bwrs}")
                print(f"    concentration    : {t2['concentration'] or '—'}")
                print(f"    avg hold period  : "
                      f"{t2['avg_hold_days']}d" if t2['avg_hold_days'] else
                      "    avg hold period  : —")
                print(f"    drawdown ratio   : "
                      f"{t2['worst_drawdown_ratio']}x" if t2['worst_drawdown_ratio']
                      else "    drawdown ratio   : —")
                print(f"    size consistency : "
                      f"cv={t2['size_cv']}" if t2['size_cv'] is not None
                      else "    size consistency : —")
                print(f"    recent trades    : {t2['recent_trades_30d']}/30d")

                if t2["passes"]:
                    print(f"    ✓ TIER 2 PASS — PRIORITY SHADOW")
                    tier2_pass.append(stats)
                else:
                    print(f"    ✗ Tier 2 fail: {', '.join(t2['reasons'])}")
                print()

                time.sleep(0.5)

    # --- Save results ---
    out_path = OUT_DIR / "verified_passers.json"
    tmp = OUT_DIR / f"verified_passers.json.tmp.{os.getpid()}"
    tmp.write_text(json.dumps(results, indent=2))
    tmp.rename(out_path)

    # --- Final summary ---
    negative = [r for r in results
                if r.get("all_time_profit") is not None
                and r["all_time_profit"] < 0]

    print(f"\n{'='*60}")
    print(f"  TIER 1 PASS  : {len(tier1_pass)}")
    if not args.tier1_only:
        print(f"  TIER 2 PASS  : {len(tier2_pass)}  ← PRIORITY SHADOW")
        t1_only = [r for r in tier1_pass
                   if r not in tier2_pass]
        print(f"  TIER 1 ONLY  : {len(t1_only)}  ← shadow with caution")
    print(f"  REJECTED     : {len(negative)} negative all-time")
    print(f"  saved: {out_path}")

    if tier2_pass:
        print(f"\n=== PRIORITY SHADOW — Tier 2 verified ({len(tier2_pass)}) ===")
        for r in sorted(tier2_pass,
                        key=lambda x: x["all_time_profit"], reverse=True):
            sd = r["screen_metrics"]
            wr = sd.get("win_rate")
            t2 = r["tier2"]
            print(f"  {r['address']}  "
                  f"all_time={fmt_pnl(r['all_time_profit'])}  "
                  f"screen_wr={f'{wr:.0%}' if wr else '—'}  "
                  f"hold={t2['avg_hold_days']}d  "
                  f"conc={t2['concentration']:.0%}  "
                  f"dd={t2['worst_drawdown_ratio']}x")

    if not args.tier1_only:
        t1_only = [r for r in tier1_pass if r not in tier2_pass]
        if t1_only:
            print(f"\n=== SHADOW WITH CAUTION — Tier 1 only ({len(t1_only)}) ===")
            for r in sorted(t1_only,
                            key=lambda x: x["all_time_profit"], reverse=True):
                sd = r["screen_metrics"]
                wr = sd.get("win_rate")
                t2 = r.get("tier2") or {}
                reasons = t2.get("reasons", [])
                print(f"  {r['address']}  "
                      f"all_time={fmt_pnl(r['all_time_profit'])}  "
                      f"screen_wr={f'{wr:.0%}' if wr else '—'}  "
                      f"t2_failed: {', '.join(reasons) or 'deep dive skipped'}")


if __name__ == "__main__":
    main()
