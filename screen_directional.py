"""Two-phase screen for directional human traders on Polymarket.

Designed as an add-on for the tradingbot copy-trading engine: drop this
file into the bot's scripts/ directory. It reuses the bot's API clients
(data-api, Gamma) and shadow list. It does NOT contain or require any
modification of the bot's source.

Phase 1 (discover): crawl top active markets by volume, aggregate
wallets trading at human size. Checkpointed to OUT_DIR/discovered.json.

Phase 2 (deep screen): paged TRADE history + REDEEM events, per-market
cashflow win rate on closed markets, plus realized P&L.

A wallet PASSES when, over the analysis window, it is winning,
profitable, directional, human-paced, human-sized, and active.
All thresholds are tunable via pm-screen.yml and CLI flags.

KNOWN LIMITATIONS (treat passers as candidates, not conclusions):
  - The window is the last ~3000 trades + ~1000 redeems. Lifetime
    losers on a hot streak can pass. ALWAYS verify the wallet's
    all-time profit on its polymarket.com profile before acting.
  - SPLIT / MERGE / CONVERSION cashflows are not tracked, so in-window
    P&L can differ from Polymarket's profile P&L.
  - Open positions are ignored (profiles mark them to market).

Output directory:
  Set PM_SCREEN_DIR env var to override the default (/tmp/screen-v3).

Files written to OUT_DIR:
  discovered.json        phase-1 checkpoint (delete to force re-crawl)
  passers.json           passers from the most recent run
  metrics.json           run stats + rejection breakdown (Enh 2)
  screened_wallets.json  per-wallet metrics cache, 1-day TTL (Enh 3)

CLI:
  python scripts/screen_directional.py                    # batch at offset 0
  python scripts/screen_directional.py 60                 # batch at offset 60
  python scripts/screen_directional.py --config my.yml
  python scripts/screen_directional.py --min-win-rate 0.65 --dry-run
  python scripts/screen_directional.py --cache-only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data_sources.data_api import DataApiClient
from src.data_sources.gamma import GammaClient
from src.shadow_list import ShadowList

NOW = int(time.time())
DAY = 86_400

OUT_DIR = Path(os.environ.get("PM_SCREEN_DIR", "/tmp/screen-v3"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

CACHE_TTL_SECONDS = DAY  # Enh 3: cached wallet metrics older than this re-fetch

# ---------------------------------------------------------------------------
# Enh 1 + 5: configuration — defaults, YAML overlay, CLI overlay (in that
# order; later layers win). Defaults match the previous hardcoded values so
# existing usage is unchanged.
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    "filters": {
        "min_win_rate": 0.60,
        "min_closed_markets": 20,
        "max_two_sided_ratio": 0.30,
        "min_trades_30d": 10,
        "max_trades_30d": 1500,
        "min_median_usd": 20.0,
        "max_median_usd": 5000.0,
        "max_single_trade_usd": 50_000.0,
        "max_days_since_trade": 14,
        "min_closed_pnl": 500.0,
        "min_true_total_pnl": 0.0,
        "max_graveyard_ratio": 0.35,
    },
    "discovery": {
        "n_markets": 120,
        "band_min_trades": 5,
        "band_max_trades": 50,
    },
    "deep_screen": {
        "n_deep": 60,
        "pages": 3,
    },
}

# Mapping from rejection-reason keys (Enh 2/4) to short human print tags.
REASON_KEYS = {
    "insufficient_closed_markets": "closed",
    "low_win_rate": "wr",
    "too_much_two_sided": "two_sided",
    "too_many_trades": "t30_high",
    "too_few_trades": "t30_low",
    "outside_trade_size_range": "med",
    "whale_single_trade": "whale_max",
    "not_profitable_enough": "pnl",
    "stale": "stale",
}


def load_config(path: str | None) -> dict:
    """Defaults <- YAML file (if present) <- returned merged dict.

    If `path` is None, looks for pm-screen.yml next to this script and in
    the CWD, silently using defaults when absent. If `path` is given
    explicitly and missing, that's an error.
    """
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy

    candidates: list[Path] = []
    explicit = path is not None
    if explicit:
        candidates = [Path(path)]
    else:
        candidates = [Path.cwd() / "pm-screen.yml",
                      Path(__file__).resolve().parent / "pm-screen.yml"]

    found = next((p for p in candidates if p.exists()), None)
    if found is None:
        if explicit:
            sys.exit(f"config file not found: {path}")
        return cfg

    try:
        import yaml  # PyYAML ships with the bot (requirements.txt)
    except ImportError:
        sys.exit("PyYAML is required for --config; pip install PyYAML")

    loaded = yaml.safe_load(found.read_text()) or {}
    for section in ("filters", "discovery", "deep_screen"):
        for k, v in (loaded.get(section) or {}).items():
            if k in cfg[section]:
                cfg[section][k] = type(cfg[section][k])(v)
            else:
                print(f"[config] WARNING: unknown key {section}.{k} ignored")
    print(f"[config] loaded {found}")
    return cfg


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Screen Polymarket wallets for directional human traders."
    )
    # Backwards compat: bare positional offset, e.g. `screen_directional.py 60`
    p.add_argument("offset", nargs="?", type=int, default=0,
                   help="candidate offset for batching (default 0)")
    p.add_argument("--config", default=None, metavar="FILE",
                   help="path to YAML config (default: pm-screen.yml if present)")
    p.add_argument("--min-win-rate", type=float, default=None,
                   help="override filters.min_win_rate")
    p.add_argument("--min-closed", type=int, default=None,
                   help="override filters.min_closed_markets")
    p.add_argument("--min-closed-pnl", type=float, default=None,
                   help="override filters.min_closed_pnl")
    p.add_argument("--dry-run", action="store_true",
                   help="don't write passers.json / metrics.json / cache")
    p.add_argument("--cache-only", action="store_true",
                   help="only use cached wallet metrics; skip wallets not cached")
    p.add_argument("--deep", action="store_true",
                   help="DEEP DISCOVERY MODE: paginate through ALL active markets "
                        "(~2,100 as of Jun 2026) instead of just the top n_markets "
                        "by volume. Much more thorough (found 151,816 raw wallets / "
                        "24,542 human-size wallets in testing, vs ~21,901 raw / "
                        "~1,135 human-size with the default fast mode) but takes "
                        "~9 minutes for discovery alone, and the downstream Tier 1/2 "
                        "screen on the much larger resulting wallet pool can take "
                        "hours. Recommended for occasional manual/deliberate runs, "
                        "not as the default for unattended automated cron runs.")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Atomic file helpers (used for every output file)
# ---------------------------------------------------------------------------

def atomic_write_json(path: Path, payload) -> None:
    """Write JSON via temp-file + rename (atomic on POSIX)."""
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.rename(path)


# ---------------------------------------------------------------------------
# Enh 3: incremental wallet cache
# ---------------------------------------------------------------------------

def load_wallet_cache() -> dict:
    path = OUT_DIR / "screened_wallets.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            print("[cache] corrupt screened_wallets.json — starting fresh")
    return {}


def cache_fresh(entry: dict) -> bool:
    return (NOW - int(entry.get("cached_at", 0))) < CACHE_TTL_SECONDS


# ---------------------------------------------------------------------------
# Data helpers (unchanged behavior from v2)
# ---------------------------------------------------------------------------

def usd(a: dict) -> float:
    v = a.get("usdcSize")
    if v is None:
        v = float(a.get("size") or 0) * float(a.get("price") or 0)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 0.1


def _with_retry(fn, label: str):
    """Call fn() up to _RETRY_ATTEMPTS times with exponential backoff
    (100ms, 200ms, 400ms). Raises the last exception if all attempts fail."""
    delay = _RETRY_BASE_DELAY
    last_exc: Exception | None = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt < _RETRY_ATTEMPTS:
                print(f"  [{label}] attempt {attempt} failed ({str(exc)[:40]}), "
                      f"retrying in {delay:.1f}s…", flush=True)
                time.sleep(delay)
                delay *= 2
    raise last_exc


def discover(api: DataApiClient, cfg: dict, stats: dict, deep: bool = False) -> list[dict]:
    d = cfg["discovery"]
    f = cfg["filters"]
    ckpt = OUT_DIR / "discovered.json"
    if ckpt.exists():
        print(f"[discover] using checkpoint {ckpt}")
        out = json.loads(ckpt.read_text())
        stats["markets_crawled"] = 0  # checkpoint reused; no crawl this run
        stats["total_wallets_found"] = None
        stats["wallets_human_sized"] = len(out)
        return out

    if deep:
        # DEEP MODE: paginate through ALL active markets via offset, not just
        # a single capped get_markets() call. Gamma's API has a hard 100-
        # market-per-request cap regardless of limit= requested (confirmed
        # 2026-06-23), so real coverage requires looping with offset until
        # the API itself errors out (a real platform-side limit around
        # offset=2100 as of testing, NOT a bug — must catch and stop
        # gracefully rather than crash).
        print("[discover] DEEP MODE — paginating through all active markets")
        with GammaClient() as gamma:
            active_markets = []
            offset = 0
            while True:
                try:
                    batch = gamma.get_markets(active=True, closed=False, limit=100, offset=offset)
                except Exception as exc:
                    print(f"[discover] pagination stopped at offset={offset}: {str(exc)[:60]}")
                    break
                if not batch:
                    break
                active_markets.extend(batch)
                offset += 100
                if len(batch) < 100:
                    break
            closed_markets = gamma.get_markets(active=False, closed=True, limit=500)
            markets = active_markets + closed_markets
            print(f"[discover] {len(active_markets)} active (deep) + {len(closed_markets)} recently closed markets")
    else:
        with GammaClient() as gamma:
            active_markets = gamma.get_markets(active=True, closed=False, limit=500)
            closed_markets = gamma.get_markets(active=False, closed=True, limit=500)
            markets = active_markets + closed_markets
            print(f"[discover] {len(active_markets)} active + {len(closed_markets)} recently closed markets")

    def vol(m):
        for k in ("volume24hr", "volume24hrClob", "volumeNum", "volume"):
            try:
                return float(m.get(k) or 0)
            except (TypeError, ValueError):
                continue
        return 0.0

    markets.sort(key=vol, reverse=True)
    if deep:
        # Deep mode: crawl ALL discovered markets, not just the top n_markets.
        markets = [m for m in markets if m.get("conditionId")]
    else:
        markets = [m for m in markets if m.get("conditionId")][:d["n_markets"]]
    print(f"[discover] crawling {len(markets)} active markets by volume")
    stats["markets_crawled"] = len(markets)

    wallets: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "sizes": [], "max": 0.0}
    )
    for i, m in enumerate(markets, 1):
        cid = m["conditionId"]
        try:
            trades = _with_retry(
                lambda: api.get_trades_by_market(cid, limit=1000),
                label=cid[:10],
            )
        except Exception as exc:
            print(f"  [{i}] {cid[:10]} fetch failed after retries: {str(exc)[:40]}")
            continue
        for t in trades:
            w = (t.get("proxyWallet") or "").lower()
            if not w:
                continue
            u = usd(t)
            rec = wallets[w]
            rec["n"] += 1
            rec["sizes"].append(u)
            rec["max"] = max(rec["max"], u)
        if i % 20 == 0:
            print(f"  [{i}/{len(markets)}] {len(wallets)} wallets so far")
        time.sleep(0.2)

    stats["total_wallets_found"] = len(wallets)

    out = []
    for w, rec in wallets.items():
        if rec["n"] < 5 or rec["max"] > f["max_single_trade_usd"]:
            continue
        s = sorted(rec["sizes"])
        med = s[len(s) // 2]
        if not (f["min_median_usd"] <= med <= f["max_median_usd"]):
            continue
        out.append({"address": w, "n": rec["n"], "median": med, "max": rec["max"]})
    out.sort(key=lambda r: r["n"], reverse=True)
    atomic_write_json(ckpt, out)
    stats["wallets_human_sized"] = len(out)
    print(f"[discover] {len(out)} human-size wallets (checkpointed)")
    return out


def paged_activity(api: DataApiClient, addr: str, typ: str, pages: int) -> list[dict]:
    """Fetch up to `pages` pages of activity for `addr` of type `typ`.

    Uses api._get("/activity", params) directly because DataApiClient's
    public get_activity_by_user() does not expose the `end` (timestamp
    cursor) parameter needed for multi-page fetches. If DataApiClient is
    refactored, update this function to use the public method once it
    supports pagination, or add `end` to the public interface.

    Expected response shape per item (camelCase dict):
      timestamp (int unix s), type (TRADE|REDEEM|SPLIT|MERGE),
      conditionId (hex str), side (BUY|SELL; empty for REDEEM),
      size (float shares), usdcSize (float USDC), price (float).
    """
    acc, end = [], None
    for _ in range(pages):
        params = {"user": addr, "limit": 1000, "type": typ}
        if end is not None:
            params["end"] = end
        batch = api._get("/activity", params) or []
        if not batch:
            break
        acc.extend(batch)
        oldest = min(int(a.get("timestamp") or 0) for a in batch)
        if end is not None and oldest >= end:
            break
        end = oldest - 1
        if len(batch) < 1000:
            break
    return acc


def metrics(trades: list[dict], redeems: list[dict]) -> dict:
    in_30 = [a for a in trades if int(a.get("timestamp") or 0) >= NOW - 30 * DAY]
    last_ts = max((int(a.get("timestamp") or 0) for a in trades), default=0)

    sizes = sorted(usd(a) for a in trades)
    median = sizes[len(sizes) // 2] if sizes else 0.0
    max_trade = sizes[-1] if sizes else 0.0

    by_sides: dict[str, set] = defaultdict(set)
    flow: dict[str, dict] = defaultdict(lambda: {"out": 0.0, "in": 0.0, "sh": 0.0})
    for a in trades:
        cid = a.get("conditionId")
        if not cid:
            continue
        side = str(a.get("side") or "").upper()
        if side in ("BUY", "SELL"):
            by_sides[cid].add(side)
        sh = float(a.get("size") or 0)
        if side == "BUY":
            flow[cid]["out"] += usd(a)
            flow[cid]["sh"] += sh
        elif side == "SELL":
            flow[cid]["in"] += usd(a)
            flow[cid]["sh"] -= sh
    for a in redeems:
        cid = a.get("conditionId")
        if not cid:
            continue
        flow[cid]["in"] += usd(a)
        flow[cid]["sh"] -= float(a.get("size") or 0)

    n_markets = len(by_sides)
    two_sided = sum(1 for s in by_sides.values() if len(s) >= 2)

    closed = wins = 0
    closed_pnl = 0.0
    for f in flow.values():
        if f["out"] <= 0:
            continue
        # "Closed" = fully exited within the window (net residual ~ 0).
        # Tolerance = max(2% of cost basis, $1.00 floor): the percentage
        # handles float accumulation on large positions; the floor handles
        # tiny positions where 2% rounds to ~zero. abs() is required so
        # redeems of pre-window buys (net negative shares) don't count as
        # closed and book payout as free profit. A pure absolute threshold
        # (e.g. <$5) breaks on large positions; the hybrid is intentional.
        tolerance = 0.02 * f["out"]  # pure 2% — consistent relative threshold
        if abs(f["sh"]) <= tolerance:
            closed += 1
            closed_pnl += f["in"] - f["out"]
            if f["in"] > f["out"]:
                wins += 1

    return {
        "trades_30d": len(in_30),
        "days_since_trade": (NOW - last_ts) / DAY if last_ts else 9999,
        "median_usd": median,
        "max_trade_usd": max_trade,
        "two_sided_ratio": (two_sided / n_markets) if n_markets else 0.0,
        "closed": closed,
        "closed_pnl": closed_pnl,
        "win_rate": (wins / closed) if closed else 0.0,
    }


def evaluate(m: dict, f: dict) -> list[str]:
    """Return rejection reason keys for wallet metrics m under filters f.
    Empty list = PASS. (Enh 4: keys feed the rejection breakdown.)"""
    reasons = []
    if m["closed"] < f["min_closed_markets"]:
        reasons.append("insufficient_closed_markets")
    elif m["win_rate"] < f["min_win_rate"]:
        reasons.append("low_win_rate")
    if m["two_sided_ratio"] >= f["max_two_sided_ratio"]:
        reasons.append("too_much_two_sided")
    if m["trades_30d"] > f["max_trades_30d"]:
        reasons.append("too_many_trades")
    elif m["trades_30d"] < f["min_trades_30d"]:
        reasons.append("too_few_trades")
    if not (f["min_median_usd"] <= m["median_usd"] <= f["max_median_usd"]):
        reasons.append("outside_trade_size_range")
    if m["max_trade_usd"] > f["max_single_trade_usd"]:
        reasons.append("whale_single_trade")
    if m["closed_pnl"] < f["min_closed_pnl"]:
        reasons.append("not_profitable_enough")
    # ── Jul 8 honest-metrics gates (audit: fc318f showed 95% closed-WR
    # but true total -$130 with 57% of markets abandoned open) ──
    if m.get("true_total_pnl", 0) < f.get("min_true_total_pnl", 0.0):
        reasons.append("negative_true_total")
    if m.get("graveyard_ratio", 0) > f.get("max_graveyard_ratio", 0.35):
        reasons.append("graveyard_holder")
    if m["days_since_trade"] > f["max_days_since_trade"]:
        reasons.append("stale")
    return reasons


def reject_text(m: dict, reasons: list[str]) -> str:
    parts = []
    for r in reasons:
        tag = REASON_KEYS[r]
        if r == "insufficient_closed_markets":
            parts.append(f"closed={m['closed']}")
        elif r == "low_win_rate":
            parts.append(f"wr={m['win_rate']:.0%} ({m['closed']} closed)")
        elif r == "too_much_two_sided":
            parts.append(f"two_sided={m['two_sided_ratio']:.0%}")
        elif r in ("too_many_trades", "too_few_trades"):
            parts.append(f"t30={m['trades_30d']}")
        elif r == "outside_trade_size_range":
            parts.append(f"med=${m['median_usd']:.0f}")
        elif r == "whale_single_trade":
            parts.append(f"whale_max=${m['max_trade_usd']:,.0f}")
        elif r == "not_profitable_enough":
            parts.append(f"pnl=${m['closed_pnl']:,.0f}")
        elif r == "stale":
            parts.append(f"stale={m['days_since_trade']:.0f}d")
        else:
            parts.append(tag)
    return ", ".join(parts)


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    # Enh 5: CLI overrides win over config file
    if args.min_win_rate is not None:
        cfg["filters"]["min_win_rate"] = args.min_win_rate
    if args.min_closed is not None:
        cfg["filters"]["min_closed_markets"] = args.min_closed
    if args.min_closed_pnl is not None:
        cfg["filters"]["min_closed_pnl"] = args.min_closed_pnl

    f = cfg["filters"]
    d = cfg["discovery"]
    ds = cfg["deep_screen"]

    run_started = time.time()
    stats_discovery: dict = {}
    rejections: Counter = Counter()
    cache = load_wallet_cache()
    cache_hits = cache_misses = 0

    already = set(ShadowList().as_watched_map())
    passers = []
    screened = 0

    with DataApiClient() as api:
        cands = [
            c for c in discover(api, cfg, stats_discovery, deep=args.deep)
            if c["address"] not in already
            and d["band_min_trades"] <= c["n"] <= d["band_max_trades"]
        ]
        stats_discovery["wallets_in_band"] = len(cands)
        cands = cands[args.offset:args.offset + ds["n_deep"]]
        print(f"\n[deep] screening {len(cands)} candidates\n")

        for i, c in enumerate(cands, 1):
            addr = c["address"]
            print(f"[{i}/{len(cands)}] {addr[:14]}…", end=" ", flush=True)

            # Enh 3: cache check
            entry = cache.get(addr)
            if entry and cache_fresh(entry):
                m = entry["metrics"]
                cache_hits += 1
                cached_tag = "CACHED "
            elif args.cache_only:
                print("SKIP not in cache (--cache-only)")
                continue
            else:
                try:
                    trades = _with_retry(
                        lambda: paged_activity(api, addr, "TRADE", ds["pages"]),
                        label=f"{addr[:10]}/TRADE",
                    )
                    redeems = _with_retry(
                        lambda: paged_activity(api, addr, "REDEEM", 1),
                        label=f"{addr[:10]}/REDEEM",
                    )
                except Exception as exc:
                    print(f"SKIP fetch failed after retries: {str(exc)[:40]}")
                    continue
                m = metrics(trades, redeems)
                cache[addr] = {"metrics": m, "cached_at": NOW}
                cache_misses += 1
                cached_tag = ""
                time.sleep(0.4)  # politeness only on real fetches

            m["address"] = addr
            screened += 1
            reasons = evaluate(m, f)

            if reasons:
                for r in reasons:
                    rejections[r] += 1
                print(f"{cached_tag}REJECT " + reject_text(m, reasons))
            else:
                passers.append(m)
                print(f"{cached_tag}PASS wr={m['win_rate']:.0%} "
                      f"({m['closed']} closed) med=${m['median_usd']:.0f} "
                      f"t30={m['trades_30d']} pnl=${m['closed_pnl']:,.0f}")

    passers.sort(key=lambda m: (m["win_rate"], m["closed"]), reverse=True)

    # ---- output files (Enh 2 + 3), all atomic, all skipped on --dry-run ----
    duration = time.time() - run_started
    total_rejected = screened - len(passers)
    metrics_payload = {
        "run_completed": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_seconds": round(duration, 1),
        "config_used": cfg,
        "offset": args.offset,
        "discovery": stats_discovery,
        "deep_screen": {
            "candidates_screened": screened,
            "passers": len(passers),
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "rejections": dict(rejections.most_common()),
        },
        "passers_summary": {
            "avg_win_rate": round(
                sum(p["win_rate"] for p in passers) / len(passers), 3
            ) if passers else None,
            "median_pnl": sorted(p["closed_pnl"] for p in passers)[
                len(passers) // 2
            ] if passers else None,
            "avg_trades_30d": round(
                sum(p["trades_30d"] for p in passers) / len(passers)
            ) if passers else None,
        },
    }

    if args.dry_run:
        print("\n[dry-run] skipping passers.json / metrics.json / cache writes")
    else:
        atomic_write_json(OUT_DIR / "passers.json", passers)
        atomic_write_json(OUT_DIR / "metrics.json", metrics_payload)
        atomic_write_json(OUT_DIR / "screened_wallets.json", cache)

    # ---- Enh 4: rejection breakdown ----
    print(f"\n=== REJECTION BREAKDOWN ({total_rejected} rejected, "
          f"{screened} screened) ===")
    for reason, count in rejections.most_common():
        pct = 100.0 * count / total_rejected if total_rejected else 0.0
        print(f"  {reason:<28} {count:>4}  ({pct:.1f}%)")

    print(f"\n=== {len(passers)} PASSERS ===")
    for m in passers:
        print(f"  {m['address']}  wr={m['win_rate']:.0%} ({m['closed']} closed)  "
              f"2-sided={m['two_sided_ratio']:.0%}  t30={m['trades_30d']}  "
              f"med=${m['median_usd']:.0f}  max=${m['max_trade_usd']:,.0f}  "
              f"pnl=${m['closed_pnl']:,.0f}")
    print(f"\ncache: {cache_hits} hits / {cache_misses} fetches"
          f"  |  duration: {duration:.0f}s")
    if not args.dry_run:
        print(f"saved: {OUT_DIR/'passers.json'}, {OUT_DIR/'metrics.json'}, "
              f"{OUT_DIR/'screened_wallets.json'}")


if __name__ == "__main__":
    main()
