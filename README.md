# Polymarket Deep Screener

A three-step pipeline that finds **directional human traders** on
Polymarket, filters out bots/whales/leaderboard frauds, and
automatically verifies their all-time profit before you shadow anyone.
Built as an add-on for the tradingbot copy-trading engine.

## Why

The Polymarket profit leaderboard is almost useless for copy trading:

- The top is **whales** ($50k–$2M single trades you can't safely mirror)
- High-volume wallets are **market-maker bots** (thousands of two-sided
  trades per day, negative or razor-thin edge)
- Profile "profit" numbers conflate realized and unrealized P&L
- A high win rate alone says nothing — a trader can win 77% of markets
  and still lose money (small wins on favorites, occasional big losses)

In live testing, **11 out of 13 screener passers had negative all-time
profit** when verified against their full profile. The pipeline below
catches all of them automatically.

## The three scripts

| Script | What it does |
|---|---|
| `screen_directional.py` | Phase 1 + 2: discovers human-sized wallets from active markets, deep-screens for win rate + realized P&L |
| `verify_passers.py` | Pulls live all-time profit, positions value, biggest win, and trade count for every passer — flags negative all-time automatically |
| `pm-screen.yml` | Config file for all thresholds — no code edits needed |

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

A wallet **passes** when it is:
- **Winning:** ≥60% win rate over ≥20 closed markets
- **Profitable:** ≥$500 realized P&L on those closed markets
- **Directional:** trades both sides in <30% of markets
- **Human-paced:** 10–1,500 trades in last 30 days
- **Human-sized:** $20–$5,000 median trade, no single trade >$50k
- **Active:** traded within the last 14 days

**Phase 3 — verify.** `verify_passers.py` fetches live profile data for
every passer — all-time profit, positions value, biggest win, prediction
count — and flags anyone with negative all-time P&L as `SKIP`.

## Install

Requires a working install of the tradingbot copy-trading engine. These
scripts import its API clients and shadow list — no bot files are
included or modified.

```bash
cp screen_directional.py verify_passers.py pm-screen.yml /opt/polymarket-bot/scripts/
cd /opt/polymarket-bot
```

## Full workflow

```bash
# Step 1: Run the screen (all batches, ~2 hours)
for off in 0 60 120 180 240 300 360 420 480 540 600 660 720 780 840; do
  echo "===== BATCH $off ====="
  .venv/bin/python scripts/screen_directional.py $off
done 2>&1 | tee /tmp/screen-v3/full-run.log

# Check who passed
grep -E "PASS|BATCH" /tmp/screen-v3/full-run.log

# Step 2: Verify passers against live profile data
.venv/bin/python scripts/verify_passers.py

# Step 3: Add survivors to shadow list
.venv/bin/python -c "
from src.shadow_list import ShadowList
sl = ShadowList()
sl.add_trader('0xADDRESS_HERE', 'name', 'screener')
print(sl.as_watched_map())
"
sudo systemctl restart tradingbot-copy-bot

# Step 4: Watch shadow P&L for 2+ weeks, then promote to live
```

## Running a single batch

```bash
.venv/bin/python scripts/screen_directional.py        # wallets 0–59
.venv/bin/python scripts/screen_directional.py 60     # wallets 60–119
```

## Verifying specific addresses directly

```bash
.venv/bin/python scripts/verify_passers.py --addresses 0xABC,0xDEF,0xGHI
```

Output:
```
#    address           pos_value  all_time_pnl  biggest_win  predictions  screen_wr  status
1    0x7eb89b08c2e8…     $39.0K        +$7.4K        $7.4K           34          —  looks promising
2    0xfe202bb8f5c8…       $819       -$39.1K          $16           30          —  NEGATIVE ALL-TIME — SKIP
```

## Configuration

Thresholds live in `pm-screen.yml`. Every key is optional — omitted keys
fall back to built-in defaults.

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
| `--cache-only` | Only evaluate wallets already in the cache |

## Output files

All outputs go to `$PM_SCREEN_DIR` (default `/tmp/screen-v3`). Set the
env var to a persistent path to survive reboots:

```bash
export PM_SCREEN_DIR=/opt/polymarket-bot/screen-results
```

| File | Contents |
|---|---|
| `passers.json` | Wallets that passed every filter, ranked by win rate |
| `verified_passers.json` | Passers with live profile data attached |
| `metrics.json` | Run stats, timing, rejection breakdown, passers summary |
| `screened_wallets.json` | Per-wallet metrics cache (1-day TTL) |
| `discovered.json` | Phase 1 checkpoint — delete to force a fresh crawl |

## Rejection breakdown

Every screen run prints which filters blocked the most candidates:

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

Use this to tune thresholds in `pm-screen.yml`.

## Known limitations

- SPLIT / MERGE / CONVERSION cashflows are not tracked; in-window P&L
  can differ from Polymarket's profile P&L
- Open positions are ignored (profiles mark them to market)
- The discovery crawl only sees wallets active in top-volume markets;
  niche-market specialists may be missed
- The wallet cache has a 1-day TTL — delete `screened_wallets.json` to
  force fresh fetches

## Version history

| Version | Changes |
|---|---|
| v1 | Initial leaderboard + subgraph win rate screen |
| v2 | Activity-feed win rates; REDEEM attribution fix; atomic writes; retry/backoff; `PM_SCREEN_DIR` env var |
| v3 | Market-crawl discovery; moderate-activity band; realized P&L filter; `abs()` closed-market fix |
| v4 | External YAML config; metrics output; wallet cache; rejection breakdown; CLI arg overrides |
| v4.1 | Added `verify_passers.py` — automatic all-time profit verification via leaderboard + positions APIs |

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

## One-command pipeline (recommended)

Instead of running screen + verify separately, use `run_pipeline.sh` to
do everything in one shot:

```bash
# Full run — all 899 wallets, screen + verify each batch automatically
./scripts/run_pipeline.sh

# Start from a specific batch (useful if resuming)
./scripts/run_pipeline.sh 60

# Run a subset of batches
./scripts/run_pipeline.sh 0 180
```

What it does for each batch:
1. Runs `screen_directional.py` (phase 1 + 2)
2. If any wallets pass, **immediately** runs `verify_passers.py` (phase 3)
3. Accumulates all verified results in `all_verified_passers.json`
4. Prints a final summary at the end

Example final output:
```
=== FINAL PIPELINE SUMMARY ===
  Total verified:  15
  Promising:       1
  Negative P&L:    13
  Unclear/error:   1

=== WORTH SHADOWING (1) ===
  0x7eb89b08c2e8…  all_time=+$7,406  screen_wr=82%  predictions=34
```

Full log saved to `$PM_SCREEN_DIR/pipeline.log`. The entire 899-wallet
run takes ~30 minutes (most wallets load from cache after the first run).
