# Polymarket Deep Screener

A four-phase pipeline that finds **directional human traders** on
Polymarket, filters out bots/whales/leaderboard frauds, and produces
two tiers of shadow candidates ranked by confidence. Built as an add-on
for the tradingbot copy-trading engine.

## Why

The Polymarket profit leaderboard is almost useless for copy trading:

- The top is **whales** ($50k–$2M single trades you can't safely mirror)
- High-volume wallets are **market-maker bots** (thousands of two-sided
  trades per day, negative or razor-thin edge)
- Profile "profit" numbers conflate realized and unrealized P&L
- A high win rate alone says nothing — a trader can win 77% of markets
  and still lose money (small wins on favorites, occasional big losses)

In live testing, **11 out of 13 screener passers had negative all-time
profit** when verified. The pipeline below catches all of them
automatically and separates the few genuine traders into two tiers.

## The four scripts

| Script | Phase | What it does |
|---|---|---|
| `screen_directional.py` | 1 + 2 | Crawls active markets, discovers human-sized wallets, deep-screens for win rate + realized P&L |
| `verify_passers.py` | 3a + 3b | Tier 1: kills negative all-time P&L. Tier 2: deep-dives survivors for consistency, conviction, and drawdown |
| `run_pipeline.sh` | All | Runs everything in one command |
| `pm-screen.yml` | Config | All thresholds in one file — no code edits needed |

## How it works

**Phase 1 — discover.** Crawls the top ~120 active markets by volume
(Gamma API), aggregates every wallet seen in recent trades, and keeps
those trading at human size ($20–$5k median clip, nothing over $50k).
Checkpointed to `discovered.json` — delete it to force a re-crawl.

**Phase 2 — deep screen.** For the moderately-active band of those
wallets (the hyperactive top is market-maker bots by construction),
pulls ~3,000 trades + redemption events per wallet, reconstructs
per-market cashflows, and computes win rate and realized P&L over
markets the wallet fully exited in-window.

A wallet **passes Phase 2** when it is:
- **Winning:** ≥60% win rate over ≥20 closed markets
- **Profitable:** ≥$500 realized P&L on those closed markets
- **Directional:** trades both sides in <30% of markets
- **Human-paced:** 10–1,500 trades in last 30 days
- **Human-sized:** $20–$5,000 median trade, no single trade >$50k
- **Active:** traded within the last 14 days

**Phase 3a — Tier 1 verify (fast).** Pulls live all-time profit,
positions value, biggest win, and prediction count from the Polymarket
API. Any wallet with negative all-time P&L is immediately flagged SKIP.

**Phase 3b — Tier 2 deep dive (slower).** Pulls ~3,000 trades of
history and runs six deeper signals on Tier 1 survivors:

| Signal | What it catches |
|---|---|
| Win rate across 3 time buckets (90d, 60d, 30d) | Single lucky streak vs consistent edge |
| Market concentration (unique mkts / total trades) | Bot spray-and-pray vs conviction |
| Average holding period | Scalpers (bad for copying) vs patient holders |
| Worst drawdown / average win ratio | Hidden tail risk |
| Trade size consistency (coefficient of variation) | Tilt, strategy changes, shared wallets |
| Recent activity (trades in last 30d) | Still actively trading |

## Output tiers

| Tier | Criteria | Action |
|---|---|---|
| **Priority Shadow** | Passed all 4 phases including Tier 2 deep dive | Shadow immediately |
| **Shadow with Caution** | Passed Phase 2 + Tier 1 only | Shadow with lower allocation, monitor closely |
| **Skip** | Negative all-time P&L | Do not shadow |

## Install

Requires a working install of the tradingbot copy-trading engine. These
scripts import its API clients and shadow list — no bot files are
included or modified.

```bash
cp screen_directional.py verify_passers.py run_pipeline.sh pm-screen.yml \
   /opt/polymarket-bot/scripts/
chmod +x /opt/polymarket-bot/scripts/run_pipeline.sh
cd /opt/polymarket-bot
```

## One-command pipeline (recommended)

```bash
# Full run — all ~900 wallets, all four phases (~30 min with cache)
./scripts/run_pipeline.sh

# Start from a specific batch (useful if resuming)
./scripts/run_pipeline.sh 60

# Run a subset of batches
./scripts/run_pipeline.sh 0 180
```

**What happens automatically:**
1. Each batch of 60 wallets is screened (phases 1+2)
2. Any passers are immediately Tier 1 verified (kills negative P&L fast)
3. After all batches complete, all Tier 1 survivors get the full Tier 2 deep dive
4. Final summary shows Priority Shadow and Shadow with Caution lists

Example final output:
```
=== PRIORITY SHADOW — Tier 2 verified ===
  0xABC…  all_time=+$12K  screen_wr=87%  hold=8.3d  conc=51%  dd=2.1x

=== SHADOW WITH CAUTION — Tier 1 only ===
  0xDEF…  all_time=+$7.4K  screen_wr=82%  t2_failed: wr_inconsistent, high_drawdown
```

## Running manually

```bash
# Screen one batch
.venv/bin/python scripts/screen_directional.py 0

# Tier 1 verify only (fast)
.venv/bin/python scripts/verify_passers.py --tier1-only

# Full Tier 1 + 2 verify on specific addresses
.venv/bin/python scripts/verify_passers.py --addresses 0xABC,0xDEF

# Skip Tier 2 for all passers
.venv/bin/python scripts/verify_passers.py --tier1-only
```

## Configuration

All thresholds live in `pm-screen.yml`. Every key is optional.

```yaml
filters:              # Phase 2 screen thresholds
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

discovery:            # Phase 1 crawl settings
  n_markets: 120
  band_min_trades: 5
  band_max_trades: 50

deep_screen:          # Phase 2 depth
  n_deep: 60
  pages: 3

tier2_filters:        # Phase 3b deep dive thresholds
  min_bucket_wr: 0.55       # win rate must hold across all time buckets
  min_concentration: 0.25   # unique markets / total trades
  min_hold_days: 1.0        # avg holding period in days
  max_drawdown_ratio: 4.0   # worst loss / avg win
  max_size_cv: 2.0          # trade size coefficient of variation
  min_recent_trades: 5      # trades in last 30 days
```

## CLI flags

```bash
.venv/bin/python scripts/screen_directional.py --min-win-rate 0.65
.venv/bin/python scripts/screen_directional.py --min-closed 25 --dry-run
.venv/bin/python scripts/screen_directional.py --cache-only
.venv/bin/python scripts/verify_passers.py --tier1-only
```

| Flag | Effect |
|---|---|
| `--config FILE` | YAML config path (default: `pm-screen.yml`) |
| `--min-win-rate F` | Override `filters.min_win_rate` |
| `--min-closed N` | Override `filters.min_closed_markets` |
| `--min-closed-pnl F` | Override `filters.min_closed_pnl` |
| `--dry-run` | Screen but write no output files |
| `--cache-only` | Only evaluate cached wallets |
| `--tier1-only` | Skip Tier 2 deep dive in verify_passers.py |

## Output files

All outputs go to `$PM_SCREEN_DIR` (default `/tmp/screen-v3`). Set to a
persistent path to survive reboots:

```bash
export PM_SCREEN_DIR=/opt/polymarket-bot/screen-results
```

| File | Contents |
|---|---|
| `passers.json` | Phase 2 passers for the current batch |
| `verified_passers.json` | Latest verify run with Tier 1 + 2 results |
| `all_verified_passers.json` | Accumulated Tier 1 survivors across all batches |
| `metrics.json` | Run stats, timing, rejection breakdown |
| `screened_wallets.json` | Per-wallet cache (1-day TTL) |
| `discovered.json` | Phase 1 checkpoint — delete to force re-crawl |
| `pipeline.log` | Full log of every pipeline run |

## Rejection breakdown

Every screen batch prints which filters blocked the most candidates:

```
=== REJECTION BREAKDOWN (55 rejected, 60 screened) ===
  not_profitable_enough          50  (90.9%)
  too_much_two_sided             35  (63.6%)
  low_win_rate                   27  (49.1%)
```

Use this to tune thresholds in `pm-screen.yml`.

## IMPORTANT: even passers need judgment

The screen + Tier 1 + Tier 2 pipeline is rigorous but not infallible:
- Priority Shadow = high confidence, but still run shadow mode 2+ weeks before going live
- Shadow with Caution = positive all-time profit but inconsistencies detected — lower allocation, watch closely
- Never promote to live without reviewing shadow P&L data

**Recommended workflow:**
1. Run `./scripts/run_pipeline.sh`
2. Add Priority Shadow wallets to the bot's shadow list
3. Optionally add Shadow with Caution wallets with reduced `base_usd`
4. Run shadow mode 2+ weeks
5. Promote to live only wallets that clear your shadow P&L bar

## Known limitations

- SPLIT / MERGE / CONVERSION cashflows not tracked; in-window P&L can
  differ from Polymarket's profile P&L
- Open positions ignored (profiles mark them to market)
- Discovery crawl only sees wallets in top-volume markets; niche-market
  specialists may be missed
- Wallet cache has 1-day TTL — delete `screened_wallets.json` to force
  fresh fetches

## Version history

| Version | Changes |
|---|---|
| v1 | Initial leaderboard + subgraph win rate screen |
| v2 | Activity-feed win rates; REDEEM attribution fix; atomic writes; retry/backoff |
| v3 | Market-crawl discovery; moderate-activity band; realized P&L filter |
| v4 | External YAML config; metrics output; wallet cache; rejection breakdown; CLI overrides |
| v4.1 | `verify_passers.py` — automatic all-time profit verification |
| v4.2 | Two-tier verification: Tier 1 (all-time profit) + Tier 2 deep dive (6 signals); `run_pipeline.sh` updated to run all phases end-to-end |

## Staying Updated

This repo is actively maintained. To update your scripts:

**Easiest way — use Claude:**
Paste the repo link into Claude and say:
> "Update my polymarket screener scripts with the latest version from this repo"

Claude will handle downloading the files and uploading them to your VPS.

**Manual way:**
1. Download any changed files from the repo
2. Upload to your VPS: `scp file.py root@YOUR_VPS_IP:/opt/polymarket-bot/scripts/`
3. No reinstall or bot restart required

## License

MIT for these scripts. This screener is an add-on — it does not include
any source from the tradingbot engine, which is sold under a separate
license that prohibits redistribution.
