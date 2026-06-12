"""Two-phase screen for directional human traders on Polymarket.

Designed as an add-on for the tradingbot copy-trading engine: drop this
file into the bot's scripts/ directory. It reuses the bot's API clients
(data-api, Gamma) and shadow list. It does NOT contain or require any
modification of the bot's source.

Phase 1 (discover): crawl top active markets by volume, aggregate
wallets trading at human size ($20-$5k median, max <= $50k single trade).
Checkpointed to OUT_DIR/discovered.json.

Phase 2 (deep screen): paged TRADE history (3 pages of 1000) + REDEEM
events, per-market cashflow win rate on closed markets (net shares ~ 0
in-window), plus realized P&L on those closed markets.

A wallet PASSES when, over the analysis window, it is:
  - winning:      win_rate >= 60% with >= 20 closed markets
  - profitable:   realized P&L on closed markets >= $500
  - directional:  trades both sides in < 30% of markets
  - human-paced:  10..1500 trades in 30d
  - human-sized:  $20 <= median trade <= $5,000, no single trade > $50k
  - active:       traded within 14 days

KNOWN LIMITATIONS (treat passers as candidates, not conclusions):
  - The window is the last ~3000 trades + ~1000 redeems. Lifetime
    losers on a hot streak can pass. ALWAYS verify the wallet's
    all-time profit on its polymarket.com profile before acting.
  - SPLIT / MERGE / CONVERSION cashflows are not tracked, so in-window
    P&L can differ from Polymarket's profile P&L.
  - Open positions are ignored (profiles mark them to market).

Output directory:
  Set PM_SCREEN_DIR env var to override the default (/tmp/screen-v3).
  Note: /tmp may be cleared on reboot; set PM_SCREEN_DIR to a persistent
  path (e.g. /opt/polymarket-bot/screen-results) to survive restarts.

CLI:
  python scripts/screen_directional.py          # first 60 candidates
  python scripts/screen_directional.py 60       # next batch, etc.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data_sources.data_api import DataApiClient
from src.data_sources.gamma import GammaClient
from src.shadow_list import ShadowList

NOW = int(time.time())
DAY = 86_400

# Fix 2: Accept PM_SCREEN_DIR env var; fall back to /tmp/screen-v3.
# Set PM_SCREEN_DIR to a persistent path to survive reboots, e.g.:
#   export PM_SCREEN_DIR=/opt/polymarket-bot/screen-results
OUT_DIR = Path(os.environ.get("PM_SCREEN_DIR", "/tmp/screen-v3"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

N_MARKETS = 120
N_DEEP = 60
PAGES = 3

MIN_WIN_RATE = 0.60
MIN_CLOSED = 20
MAX_TWO_SIDED = 0.30
MIN_TRADES_30D = 10
MAX_TRADES_30D = 1500
MIN_MEDIAN_USD = 20.0
MAX_MEDIAN_USD = 5000.0
MAX_SINGLE_TRADE_USD = 50_000.0
MAX_DAYS_SINCE_TRADE = 14
MIN_CLOSED_PNL = 500.0

# Deep-screen the moderately-active band of discovered wallets.
# The hyperactive top of the list is market-maker bots by construction.
BAND_MIN_TRADES = 5
BAND_MAX_TRADES = 50

# Fix 3: Retry settings for transient API errors.
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 0.1  # seconds; doubles each attempt (100ms, 200ms, 400ms)


def usd(a: dict) -> float:
    v = a.get("usdcSize")
    if v is None:
        v = float(a.get("size") or 0) * float(a.get("price") or 0)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _with_retry(fn, label: str):
    """Call fn() up to _RETRY_ATTEMPTS times with exponential backoff.

    Retries on any exception. Raises the last exception if all attempts fail.
    Used for API calls that may hit transient network errors or rate limits.
    """
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


def discover(api: DataApiClient) -> list[dict]:
    ckpt = OUT_DIR / "discovered.json"
    if ckpt.exists():
        print(f"[discover] using checkpoint {ckpt}")
        return json.loads(ckpt.read_text())

    with GammaClient() as gamma:
        markets = gamma.get_markets(active=True, closed=False, limit=500)

    def vol(m):
        for k in ("volume24hr", "volume24hrClob", "volumeNum", "volume"):
            try:
                return float(m.get(k) or 0)
            except (TypeError, ValueError):
                continue
        return 0.0

    markets.sort(key=vol, reverse=True)
    markets = [m for m in markets if m.get("conditionId")][:N_MARKETS]
    print(f"[discover] crawling {len(markets)} active markets by volume")

    wallets: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "sizes": [], "max": 0.0}
    )
    for i, m in enumerate(markets, 1):
        cid = m["conditionId"]
        try:
            # Fix 3: retry market trade fetches on transient errors.
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

    out = []
    for w, rec in wallets.items():
        if rec["n"] < 5 or rec["max"] > MAX_SINGLE_TRADE_USD:
            continue
        s = sorted(rec["sizes"])
        med = s[len(s) // 2]
        if not (MIN_MEDIAN_USD <= med <= MAX_MEDIAN_USD):
            continue
        out.append({"address": w, "n": rec["n"], "median": med, "max": rec["max"]})
    out.sort(key=lambda r: r["n"], reverse=True)

    # Fix 1: Atomic checkpoint write — write to a temp file in the same
    # directory, then rename. rename() is atomic on POSIX; prevents a
    # partially-written file being read by a concurrent process.
    tmp = OUT_DIR / f"discovered.json.tmp.{os.getpid()}"
    tmp.write_text(json.dumps(out))
    tmp.rename(ckpt)

    print(f"[discover] {len(out)} human-size wallets (checkpointed)")
    return out


def paged_activity(api: DataApiClient, addr: str, typ: str, pages: int) -> list[dict]:
    """Fetch up to `pages` pages of activity for `addr` of type `typ`.

    Fix 4: Documents the private API dependency.

    Uses api._get("/activity", params) directly because DataApiClient's
    public get_activity_by_user() does not expose the `end` (timestamp
    cursor) parameter needed for multi-page fetches. If DataApiClient is
    refactored, update this function to use the public method once it
    supports pagination, or add `end` to the public interface.

    Expected response shape per item (camelCase dict):
      - timestamp:   int unix seconds
      - type:        str "TRADE" | "REDEEM" | "SPLIT" | "MERGE"
      - conditionId: str hex market ID
      - side:        str "BUY" | "SELL" (TRADE only; empty for REDEEM)
      - size:        float shares
      - usdcSize:    float USDC notional (present on activity endpoint;
                     absent on /trades — that's why we use /activity)
      - price:       float (0.0 for REDEEMs)
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
        # Fix 5: "Closed" means the trader fully exited this market
        # within the analysis window (net residual shares ~ zero).
        #
        # Tolerance = max(2% of cost basis, $1.00 floor).
        #
        # The percentage component (2% of out) handles large positions
        # where floating-point accumulation across many fills can leave
        # a small fractional residual; without it, a $5,000 position
        # with a $0.50 rounding tail would be wrongly excluded.
        #
        # The $1.00 floor handles tiny positions ($5–$10 bets) where
        # 2% of cost basis rounds to near zero and is too strict.
        #
        # abs() is required: redeems for shares bought BEFORE the window
        # drive net shares negative; without abs() those would pass the
        # check and attribute redemption proceeds as free profit (no
        # matching buy cost in the window).
        #
        # Alternative: a pure absolute threshold (e.g. residual < $5)
        # would be simpler but breaks on large positions where rounding
        # routinely exceeds $5. The hybrid is intentional.
        tolerance = max(0.02 * f["out"], 1.0)
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


def main() -> None:
    already = set(ShadowList().as_watched_map())
    passers = []
    with DataApiClient() as api:
        cands = [
            c for c in discover(api)
            if c["address"] not in already
            and BAND_MIN_TRADES <= c["n"] <= BAND_MAX_TRADES
        ]
        offset = int(sys.argv[1]) if len(sys.argv) > 1 else 0
        cands = cands[offset:offset + N_DEEP]
        print(f"\n[deep] screening {len(cands)} candidates\n")
        for i, c in enumerate(cands, 1):
            addr = c["address"]
            print(f"[{i}/{len(cands)}] {addr[:14]}…", end=" ", flush=True)
            try:
                # Fix 3: retry paged activity fetches on transient errors.
                trades = _with_retry(
                    lambda: paged_activity(api, addr, "TRADE", PAGES),
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
            m["address"] = addr

            reasons = []
            if m["closed"] < MIN_CLOSED:
                reasons.append(f"closed={m['closed']}")
            elif m["win_rate"] < MIN_WIN_RATE:
                reasons.append(f"wr={m['win_rate']:.0%} ({m['closed']} closed)")
            if m["two_sided_ratio"] >= MAX_TWO_SIDED:
                reasons.append(f"two_sided={m['two_sided_ratio']:.0%}")
            if not (MIN_TRADES_30D <= m["trades_30d"] <= MAX_TRADES_30D):
                reasons.append(f"t30={m['trades_30d']}")
            if not (MIN_MEDIAN_USD <= m["median_usd"] <= MAX_MEDIAN_USD):
                reasons.append(f"med=${m['median_usd']:.0f}")
            if m["max_trade_usd"] > MAX_SINGLE_TRADE_USD:
                reasons.append(f"whale_max=${m['max_trade_usd']:,.0f}")
            if m["closed_pnl"] < MIN_CLOSED_PNL:
                reasons.append(f"pnl=${m['closed_pnl']:,.0f}")
            if m["days_since_trade"] > MAX_DAYS_SINCE_TRADE:
                reasons.append(f"stale={m['days_since_trade']:.0f}d")

            if reasons:
                print("REJECT " + ", ".join(reasons))
            else:
                passers.append(m)
                print(f"PASS wr={m['win_rate']:.0%} ({m['closed']} closed) "
                      f"med=${m['median_usd']:.0f} t30={m['trades_30d']} "
                      f"pnl=${m['closed_pnl']:,.0f}")
            time.sleep(0.4)

    passers.sort(key=lambda m: (m["win_rate"], m["closed"]), reverse=True)

    # Fix 1: Atomic write for passers.json too.
    out_path = OUT_DIR / "passers.json"
    tmp = OUT_DIR / f"passers.json.tmp.{os.getpid()}"
    tmp.write_text(json.dumps(passers, indent=1))
    tmp.rename(out_path)

    print(f"\n=== {len(passers)} PASSERS ===")
    for m in passers:
        print(f"  {m['address']}  wr={m['win_rate']:.0%} ({m['closed']} closed)  "
              f"2-sided={m['two_sided_ratio']:.0%}  t30={m['trades_30d']}  "
              f"med=${m['median_usd']:.0f}  max=${m['max_trade_usd']:,.0f}  "
              f"pnl=${m['closed_pnl']:,.0f}")
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    main()
