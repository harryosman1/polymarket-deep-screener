"""Two-phase screen for directional human traders on Polymarket.

Designed as an add-on for the tradingbot copy-trading engine: drop this
file into the bot's scripts/ directory. It reuses the bot's API clients
(data-api, Gamma) and shadow list. It does NOT contain or require any
modification of the bot's source.

Phase 1 (discover): crawl top active markets by volume, aggregate
wallets trading at human size ($20-$5k median, max <= $50k single trade).
Checkpointed to /tmp/screen-v3/discovered.json.

Phase 2 (deep screen): paged TRADE history (3 pages of 1000) + REDEEM
events, per-market cashflow win rate on closed markets (net shares ~ 0
in-window), plus realized P&L on those closed markets.

A wallet PASSES when, over the analysis window, it is:
  - winning:      win_rate >= 60% with >= 15 closed markets
  - profitable:   realized P&L on closed markets > 0
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

CLI:
  python scripts/screen_directional.py          # first 60 candidates
  python scripts/screen_directional.py 60       # next batch, etc.
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data_sources.data_api import DataApiClient
from src.data_sources.gamma import GammaClient
from src.shadow_list import ShadowList

NOW = int(time.time())
DAY = 86_400
OUT_DIR = Path("/tmp/screen-v3")
OUT_DIR.mkdir(parents=True, exist_ok=True)

N_MARKETS = 120
N_DEEP = 60
PAGES = 3

MIN_WIN_RATE = 0.60
MIN_CLOSED = 15
MAX_TWO_SIDED = 0.30
MIN_TRADES_30D = 10
MAX_TRADES_30D = 1500
MIN_MEDIAN_USD = 20.0
MAX_MEDIAN_USD = 5000.0
MAX_SINGLE_TRADE_USD = 50_000.0
MAX_DAYS_SINCE_TRADE = 14

# Deep-screen the moderately-active band of discovered wallets.
# The hyperactive top of the list is market-maker bots by construction.
BAND_MIN_TRADES = 5
BAND_MAX_TRADES = 50


def usd(a: dict) -> float:
    v = a.get("usdcSize")
    if v is None:
        v = float(a.get("size") or 0) * float(a.get("price") or 0)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


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
            trades = api.get_trades_by_market(cid, limit=1000)
        except Exception as exc:
            print(f"  [{i}] {cid[:10]} fetch failed: {str(exc)[:40]}")
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
    ckpt.write_text(json.dumps(out))
    print(f"[discover] {len(out)} human-size wallets (checkpointed)")
    return out


def paged_activity(api: DataApiClient, addr: str, typ: str, pages: int) -> list[dict]:
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
        # closed = in-window net shares ~ zero. abs() matters: redeems of
        # shares bought BEFORE the window drive net negative and must not
        # count as closed (their buy cost is outside the window).
        if abs(f["sh"]) <= max(0.02 * f["out"], 1.0):
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
                trades = paged_activity(api, addr, "TRADE", PAGES)
                redeems = paged_activity(api, addr, "REDEEM", 1)
            except Exception as exc:
                print(f"SKIP fetch failed: {str(exc)[:40]}")
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
            if m["closed_pnl"] <= 0:
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
    (OUT_DIR / "passers.json").write_text(json.dumps(passers, indent=1))
    print(f"\n=== {len(passers)} PASSERS ===")
    for m in passers:
        print(f"  {m['address']}  wr={m['win_rate']:.0%} ({m['closed']} closed)  "
              f"2-sided={m['two_sided_ratio']:.0%}  t30={m['trades_30d']}  "
              f"med=${m['median_usd']:.0f}  max=${m['max_trade_usd']:,.0f}  "
              f"pnl=${m['closed_pnl']:,.0f}")
    print(f"\nsaved: {OUT_DIR/'passers.json'}")


if __name__ == "__main__":
    main()
