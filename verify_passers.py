"""verify_passers.py — Pull live profile stats for every passer in passers.json.

Prints and saves a table showing:
  - Positions value (open book, mark-to-market)
  - All-time profit
  - Biggest win
  - Total predictions (trade count)

Designed to replace the manual "open each profile on polymarket.com" step.
Passers that show negative all-time profit are flagged automatically.

CLI:
  python scripts/verify_passers.py
  python scripts/verify_passers.py --passers /path/to/passers.json
  python scripts/verify_passers.py --addresses 0xABC,0xDEF
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data_sources.data_api import DataApiClient

OUT_DIR = Path(os.environ.get("PM_SCREEN_DIR", "/tmp/screen-v3"))


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


def get_profile_stats(api: DataApiClient, address: str) -> dict:
    """Fetch profile stats for a single address.

    Uses three sources:
    1. Polymarket leaderboard portfolio API (all-time profit, trade count)
    2. data-api /positions (positions value, cash P&L, biggest win)
    3. data-api /profile fallback
    """
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

    # Source 3: /profile fallback
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify passer profiles")
    parser.add_argument("--passers", default=str(OUT_DIR / "passers.json"),
                        help="Path to passers.json")
    parser.add_argument("--addresses", default=None,
                        help="Comma-separated addresses to check directly")
    args = parser.parse_args()

    if args.addresses:
        addresses = [a.strip().lower() for a in args.addresses.split(",")]
        screen_data = {a: {} for a in addresses}
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

    print(f"\nVerifying {len(addresses)} wallets...\n")
    print(f"{'#':<4} {'address':<16} {'pos_value':>10} {'all_time_pnl':>13} "
          f"{'biggest_win':>12} {'predictions':>12}  {'screen_wr':>9}  status")
    print("-" * 105)

    results = []
    with DataApiClient() as api:
        for i, addr in enumerate(addresses, 1):
            stats = get_profile_stats(api, addr)
            sd = screen_data.get(addr, {})

            atp = stats["all_time_profit"]
            if stats["error"] and atp is None:
                status = f"ERROR: {stats['error']}"
            elif atp is not None and atp < 0:
                status = "NEGATIVE ALL-TIME — SKIP"
            elif atp is not None and atp < 500:
                status = "LOW ALL-TIME — VERIFY CAREFULLY"
            elif atp is None:
                status = "no profit data — check manually"
            else:
                status = "looks promising"

            wr = sd.get("win_rate")
            wr_str = f"{wr:.0%}" if wr is not None else "—"

            print(f"{i:<4} {addr[:14]}… {fmt_usd(stats['positions_value']):>10} "
                  f"{fmt_pnl(atp):>13} {fmt_usd(stats['biggest_win']):>12} "
                  f"{str(stats['predictions'] or '—'):>12}  {wr_str:>9}  {status}")

            stats["screen_metrics"] = sd
            results.append(stats)
            time.sleep(0.3)

    out_path = OUT_DIR / "verified_passers.json"
    tmp = OUT_DIR / f"verified_passers.json.tmp.{os.getpid()}"
    tmp.write_text(json.dumps(results, indent=2))
    tmp.rename(out_path)

    promising = [r for r in results
                 if r.get("all_time_profit") is not None
                 and r["all_time_profit"] >= 500]
    negative = [r for r in results
                if r.get("all_time_profit") is not None
                and r["all_time_profit"] < 0]

    print(f"\n{'='*60}")
    print(f"  {len(promising)} promising  |  {len(negative)} negative all-time  "
          f"|  {len(results) - len(promising) - len(negative)} unclear/error")
    print(f"  saved: {out_path}")

    if promising:
        print(f"\n=== WORTH SHADOWING ({len(promising)}) ===")
        for r in sorted(promising,
                        key=lambda x: x["all_time_profit"], reverse=True):
            sd = r["screen_metrics"]
            wr = sd.get("win_rate")
            print(f"  {r['address']}  "
                  f"all_time={fmt_pnl(r['all_time_profit'])}  "
                  f"screen_wr={f'{wr:.0%}' if wr else '—'}  "
                  f"predictions={r['predictions'] or '—'}")


if __name__ == "__main__":
    main()
