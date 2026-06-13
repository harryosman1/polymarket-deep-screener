# Polymarket Deep Screener

A two-phase wallet screener that finds **directional human traders** on
Polymarket — and filters out the bots, market makers, and whales that
dominate the leaderboards. Built as an add-on for the tradingbot
copy-trading engine.

## Why

The Polymarket profit leaderboard is almost useless for copy trading:

- The top is **whales** ($50k–$2M single trades you can't safely mirror)
- High-volume wallets are **market-maker bots** (thousands of two-sided
  trades per day, negative or razor-thin edge)
- Profile "profit" numbers conflate realized and unrealized P&L
- A high win rate alone says nothing — a trader can win 77% of markets
  and still lose money (small wins on favorites, occasional big losses)

This screener measures what actually matters: **recent realized win rate
AND realized profit on closed markets, at human trade sizes, with
one-directional conviction.**

## How it works

**Phase 1 — discover.** Crawls the top ~120 active markets by volume
(Gamma API), aggregates every wallet seen in recent trades, and keeps
those trading at human size ($20–$5k median clip, nothing over $50k).
Checkpointed to `discovered.json` — delete it to force a re-crawl.

**Phase 2 — deep screen.** For the moderately-active band of those
wallets (the hyperactive top is market-maker bots by construction),
pulls ~3,000 trades + redemption events per wallet via timestamp-paged
requests, reconstructs per-market cashflows, and computes win rate and
realized P&L over markets the wallet fully exited in-window.

A wallet **passes** when it is:
- **Winning:** ≥60% win rate over ≥20 closed markets
- **Profitable:** ≥$500 realized P&L on those closed markets
- **Directional:** trades both sides in <30% of markets
- **Human-paced:** 10–1,500 trades in last 30 days
- **Human-sized:** $20–$5,000 median trade, no single trade >$50k
- **Active:** traded within the last 14 days

## Install

Requires a working install of the tradingbot copy-trading engine. This
script imports its API clients and shadow list — no bot files are
included or modified here.

```bash
cp screen_directional.py pm-screen.yml /opt/polymarket-bot/scripts/
cd /opt/polymarket-bot
```

## Usage

```bash
# Run first batch (wallets 0–59)
.venv/bin/python scripts/screen_directional.py

# Run subsequent batches
.venv/bin/python scripts/screen_directional.py 60
.venv/bin/python scripts/screen_directional.py 120

# Run all batches unattended (~2 hours)
for off in 0 60 120 180 240 300 360 420 480 540 600 660 720 780 840; do
  echo "===== BATCH $off ====="
  .venv/bin/python scripts/screen_directional.py $off
done 2>&1 | tee /tmp/screen-v3/full-run.log

# Check passers across all batches
grep -E "PASS|BATCH" /tmp/screen-v3/full-run.log
```

## Configuration

Thresholds live in `pm-screen.yml` — copy it next to the script or pass
`--config path/to/file.yml`. Every key is optional; omitted keys fall
back to built-in defaults.

```yaml
filters:
  min_win_rate: 0.60
  min_closed_markets: 20
  max_two_sided_ratio: 0.30
  min_trades_30d: 10
  max_trades_30d: 1500
  min_median_usd: 20.0
  max_median_usd: 5000.0
  max_single_trade_usd: 50000.0
  max_days_since_trade: 14
  min_closed_pnl: 500.0

discovery:
  n_markets: 120
  band_min_trades: 5
  band_max_trades: 50

deep_screen:
  n_deep: 60
  pages: 3
```

## CLI flags

CLI flags override the config file for fast threshold iteration without
editing any file:

```bash
.venv/bin/python scripts/screen_directional.py --min-win-rate 0.65
.venv/bin/python scripts/screen_directional.py --min-closed 25 --dry-run
.venv/bin/python scripts/screen_directional.py --cache-only
```

| Flag | Effect |
|---|---|
| `--config FILE` | YAML config path (default: `pm-screen.yml`) |
| `--min-win-rate F` | Override `filters.min_win_rate` |
| `--min-closed N` | Override `filters.min_closed_markets` |
| `--min-closed-pnl F` | Override `filters.min_closed_pnl` |
| `--dry-run` | Screen but write no output files |
| `--cache-only` | Only evaluate wallets already in the cache; no fresh fetches |

## Output files

All outputs go to `$PM_SCREEN_DIR` (default `/tmp/screen-v3`). Set the
env var to a persistent path to survive reboots:

```bash
export PM_SCREEN_DIR=/opt/polymarket-bot/screen-results
```

| File | Contents |
|---|---|
| `passers.json` | Wallets that passed every filter, ranked by win rate |
| `metrics.json` | Run stats, timing, rejection breakdown, passers summary |
| `screened_wallets.json` | Per-wallet metrics cache (1-day TTL); repeat runs reuse fresh entries and print `CACHED` |
| `discovered.json` | Phase 1 checkpoint — delete to force a fresh market crawl |

## Rejection breakdown

Every run prints a breakdown of which filters blocked candidates — the
primary tool for tuning thresholds:

```
=== REJECTION BREAKDOWN (57 rejected, 60 screened) ===
  not_profitable_enough          39  (65.0%)
  too_much_two_sided             36  (60.0%)
  low_win_rate                   27  (45.0%)
  insufficient_closed_markets    24  (40.0%)
  too_many_trades                22  (36.7%)
  outside_trade_size_range       18  (30.0%)
  whale_single_trade              5   (8.3%)
```

The breakdown is also saved to `metrics.json` for programmatic use.

## IMPORTANT: passers are candidates, not conclusions

The screen analyzes a recent window (~3,000 trades). A lifetime loser on
a hot streak can pass. **Always open the wallet's polymarket.com profile
and verify positive all-time profit and months of history before
shadowing or copying anyone.**

In live testing, profile verification rejected the majority of passers.
The screen is a filter, not a verdict.

**Recommended workflow:**
1. Screen → get passers list
2. Verify each on `polymarket.com/profile/0x...`
3. Add survivors to the bot's shadow list
4. Run shadow mode for 2+ weeks
5. Only then promote to live

## Known limitations

- SPLIT / MERGE / CONVERSION cashflows are not tracked; in-window P&L
  can differ from Polymarket's profile P&L
- Open positions are ignored (profiles mark them to market)
- The discovery crawl only sees wallets active in top-volume markets;
  niche-market specialists may be missed
- The wallet cache has a 1-day TTL — if a trader's behavior changed
  overnight, run with a fresh cache (`--cache-only` off, delete
  `screened_wallets.json`)

## Version history

| Version | Changes |
|---|---|
| v1 | Initial leaderboard + subgraph win rate screen |
| v2 | Switched to activity-feed win rates; fixed REDEEM attribution; atomic writes; retry/backoff; `PM_SCREEN_DIR` env var |
| v3 | Market-crawl discovery; moderate-activity band; realized P&L filter; `abs()` closed-market fix |
| v4 | External YAML config; metrics/observability output; wallet cache (1-day TTL); rejection breakdown; CLI arg overrides |

## License

MIT for this script. This screener is an add-on — it does not include
any source from the tradingbot engine, which is sold under a separate
license that prohibits redistribution.
