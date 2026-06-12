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
- Profile "profit" numbers conflate realized and unrealized P&L, and a
  high win rate alone says nothing — a trader can win 77% of markets
  and still lose money (small wins on favorites, occasional big losses)

This screener measures what actually matters for copying: **recent
realized win rate AND realized profit on closed markets, at human trade
sizes, with one-directional conviction.**

## How it works

**Phase 1 — discover.** Crawls the ~120 highest-volume active markets
(Gamma API), aggregates every wallet seen in recent trades, and keeps
those trading at human size: $20–$5,000 median clip, no single trade
over $50k. Results are checkpointed.

**Phase 2 — deep screen.** For the moderately-active band of those
wallets (the hyperactive top is bots by construction), pulls ~3,000
trades + redemption events per wallet via timestamp-paged requests,
reconstructs per-market cashflows, and computes win rate and realized
P&L over markets the wallet fully exited in-window.

A wallet passes when it is winning (≥60% over ≥15 closed markets),
profitable (realized P&L > 0), directional (<30% two-sided),
human-paced (10–1,500 trades/30d), human-sized, and active (≤14 days).

## Install

Requires a working install of the tradingbot copy-trading engine
(this script imports its API clients and shadow list — no bot files are
included or modified here).

```bash
cp screen_directional.py /opt/polymarket-bot/scripts/
cd /opt/polymarket-bot
.venv/bin/python scripts/screen_directional.py        # batch 1
.venv/bin/python scripts/screen_directional.py 60     # batch 2, etc.
```

Run all batches unattended:

```bash
for off in 0 60 120 180 240 300 360 420 480 540 600 660 720 780 840; do
  echo "===== BATCH $off ====="
  .venv/bin/python scripts/screen_directional.py $off
done 2>&1 | tee /tmp/screen-v3/full-run.log

grep -E "PASS|BATCH" /tmp/screen-v3/full-run.log
```

Passers are also saved to `/tmp/screen-v3/passers.json`.

## IMPORTANT: passers are candidates, not conclusions

The screen analyzes a recent window (last ~3,000 trades). A lifetime
loser on a hot streak can pass. **Always open the wallet's
polymarket.com profile and verify positive all-time profit and a
months-long history before shadowing or copying anyone.** In testing,
profile verification rejected 2 of the first 3 passers.

Recommended workflow: screen → verify profile → add to the bot's
shadow list → judge 2+ weeks of shadow P&L → only then promote to live.

## Known limitations

- SPLIT / MERGE / CONVERSION cashflows are not tracked; in-window P&L
  can differ from Polymarket's profile P&L.
- Open positions are ignored (profiles mark them to market).
- The discovery crawl only sees wallets active in top-volume markets
  during the crawl; niche-market specialists may be missed.

## License

MIT for this script. Note: this screener is an add-on; it does not
include any source from the tradingbot engine, which is sold under its
own license that prohibits redistribution of its code.
