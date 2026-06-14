#!/usr/bin/env python3
"""network_follow.py — Find wallets co-trading with your shadow roster.

Looks at every market your shadow wallets have traded recently, finds
other wallets trading the same markets, and screens them for win rate
and profitability. Surfaces hidden gems that the main discovery crawl
misses because they don't appear in the top-100-volume markets.

Usage:
  python scripts/network_follow.py
  python scripts/network_follow.py --min-win-rate 0.65
  python scripts/network_follow.py --top 20
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data_sources.data_api import DataApiClient
from src.shadow_list import ShadowList

OUT_DIR = Path(os.environ.get("PM_SCREEN_DIR", "/tmp/screen-v3"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

NOW = int(time.time())
DAY = 86_400

# Thresholds
MIN_WIN_RATE   = 0.60
MIN_CLOSED     = 10
MIN_PNL        = 200.0
MAX_TWO_SIDED  = 0.35
MIN_MEDIAN_USD = 10.0
MAX_MEDIAN_USD = 5000.0
LOOKBACK_DAYS  = 30
TOP_N          = 30


def usd(a: dict) -> float:
    v = a.get("usdcSize")
    if v is None:
        v = float(a.get("size") or 0) * float(a.get("price") or 0)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def get_shadow_markets(api: DataApiClient) -> set[str]:
    """Get all condition IDs traded by shadow wallets in last 30 days."""
    shadow = ShadowList()
    traders = shadow.list_all()
    markets = set()

    print(f"[network] scanning {len(traders)} shadow wallets for recent markets...")
    for t in traders:
        try:
            activity = api._get("/activity", {
                "user": t.address, "limit": 500, "type": "TRADE"
            }) or []
            cutoff = NOW - LOOKBACK_DAYS * DAY
            recent = [a for a in activity
                      if int(a.get("timestamp") or 0) >= cutoff]
            cids = {a["conditionId"] for a in recent if a.get("conditionId")}
            markets |= cids
            print(f"  {t.name}: {len(cids)} markets in last {LOOKBACK_DAYS}d")
            time.sleep(0.3)
        except Exception as exc:
            print(f"  {t.name}: error — {str(exc)[:50]}")

    print(f"[network] found {len(markets)} unique markets to scan")
    return markets


def find_co_traders(api: DataApiClient, markets: set[str],
                    shadow_addrs: set[str]) -> dict[str, dict]:
    """Find wallets co-trading in the same markets."""
    wallets: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "sizes": [], "markets": set()}
    )

    for i, cid in enumerate(markets, 1):
        try:
            trades = api.get_trades_by_market(cid, limit=1000)
            for t in trades:
                w = (t.get("proxyWallet") or "").lower()
                if not w or w in shadow_addrs:
                    continue
                u = usd(t)
                if u < MIN_MEDIAN_USD:
                    continue
                wallets[w]["n"] += 1
                wallets[w]["sizes"].append(u)
                wallets[w]["markets"].add(cid)
            if i % 10 == 0:
                print(f"  [{i}/{len(markets)}] {len(wallets)} co-traders found")
            time.sleep(0.2)
        except Exception as exc:
            print(f"  market {cid[:10]} error: {str(exc)[:40]}")

    return wallets


def screen_wallet(api: DataApiClient, addr: str) -> dict | None:
    """Quick screen — pull activity and compute basic metrics."""
    try:
        trades = api._get("/activity", {
            "user": addr, "limit": 1000, "type": "TRADE"
        }) or []
        redeems = api._get("/activity", {
            "user": addr, "limit": 200, "type": "REDEEM"
        }) or []
    except Exception:
        return None

    if len(trades) < 5:
        return None

    cutoff = NOW - LOOKBACK_DAYS * DAY
    recent = [t for t in trades if int(t.get("timestamp") or 0) >= cutoff]
    sizes = sorted(usd(t) for t in trades if usd(t) > 0)
    if not sizes:
        return None
    median = sizes[len(sizes) // 2]
    if not (MIN_MEDIAN_USD <= median <= MAX_MEDIAN_USD):
        return None

    # cashflows
    flow: dict[str, dict] = defaultdict(
        lambda: {"out": 0.0, "in": 0.0, "sh": 0.0}
    )
    by_sides: dict[str, set] = defaultdict(set)
    for a in trades:
        cid = a.get("conditionId")
        if not cid:
            continue
        side = str(a.get("side") or "").upper()
        sh = float(a.get("size") or 0)
        if side == "BUY":
            flow[cid]["out"] += usd(a)
            flow[cid]["sh"] += sh
            by_sides[cid].add("BUY")
        elif side == "SELL":
            flow[cid]["in"] += usd(a)
            flow[cid]["sh"] -= sh
            by_sides[cid].add("SELL")
    for a in redeems:
        cid = a.get("conditionId")
        if not cid:
            continue
        flow[cid]["in"] += usd(a)
        flow[cid]["sh"] -= float(a.get("size") or 0)

    n_mkts = len(by_sides)
    two_sided = sum(1 for s in by_sides.values() if len(s) >= 2)
    two_sided_ratio = (two_sided / n_mkts) if n_mkts else 0

    closed = wins = 0
    pnl = 0.0
    for f in flow.values():
        if f["out"] <= 0:
            continue
        if abs(f["sh"]) <= 0.02 * f["out"]:
            closed += 1
            pnl += f["in"] - f["out"]
            if f["in"] > f["out"]:
                wins += 1

    if closed < MIN_CLOSED:
        return None
    win_rate = wins / closed
    if win_rate < MIN_WIN_RATE:
        return None
    if two_sided_ratio > MAX_TWO_SIDED:
        return None
    if pnl < MIN_PNL:
        return None

    return {
        "address": addr,
        "win_rate": round(win_rate, 3),
        "closed": closed,
        "pnl": round(pnl, 2),
        "median_usd": round(median, 2),
        "trades_30d": len(recent),
        "two_sided_ratio": round(two_sided_ratio, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Network follow screener")
    parser.add_argument("--min-win-rate", type=float, default=MIN_WIN_RATE)
    parser.add_argument("--top", type=int, default=TOP_N)
    args = parser.parse_args()

    shadow = ShadowList()
    shadow_addrs = {t.address.lower() for t in shadow.list_all()}

    print(f"\n[network] shadow wallets: {len(shadow_addrs)}")
    print(f"[network] looking for co-traders in their last {LOOKBACK_DAYS}d markets\n")

    passers = []
    with DataApiClient() as api:
        markets = get_shadow_markets(api)
        if not markets:
            print("No markets found — shadow wallets may not have traded recently.")
            return

        co_traders = find_co_traders(api, markets, shadow_addrs)
        print(f"\n[network] {len(co_traders)} unique co-traders found")

        # Sort by number of shared markets (most overlap first)
        ranked = sorted(
            co_traders.items(),
            key=lambda x: len(x[1]["markets"]),
            reverse=True
        )[:args.top * 3]  # screen top candidates

        print(f"[network] screening top {len(ranked)} by market overlap...\n")
        for i, (addr, rec) in enumerate(ranked, 1):
            print(f"[{i}/{len(ranked)}] {addr[:14]}… "
                  f"({len(rec['markets'])} shared markets)", end=" ", flush=True)
            result = screen_wallet(api, addr)
            if result:
                result["shared_markets"] = len(rec["markets"])
                passers.append(result)
                print(f"PASS wr={result['win_rate']:.0%} "
                      f"({result['closed']} closed) "
                      f"pnl=${result['pnl']:,.0f} "
                      f"med=${result['median_usd']:.0f}")
            else:
                print("reject")
            time.sleep(0.4)

    passers.sort(key=lambda r: (r["win_rate"], r["pnl"]), reverse=True)

    out = OUT_DIR / "network_passers.json"
    tmp = OUT_DIR / f"network_passers.json.tmp.{os.getpid()}"
    tmp.write_text(json.dumps(passers, indent=2))
    tmp.rename(out)

    print(f"\n=== NETWORK PASSERS ({len(passers)}) ===")
    for r in passers:
        print(f"  {r['address']}  "
              f"wr={r['win_rate']:.0%} ({r['closed']} closed)  "
              f"shared={r['shared_markets']}  "
              f"pnl=${r['pnl']:,.0f}  "
              f"med=${r['median_usd']:.0f}  "
              f"t30={r['trades_30d']}")
    print(f"\nsaved: {out}")
    print("\nNext: run verify_passers.py --addresses on these wallets")


if __name__ == "__main__":
    main()
