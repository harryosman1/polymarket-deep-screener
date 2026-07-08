#!/usr/bin/env bash
# run_pipeline.sh — Full three-phase screen + verify pipeline.
#
# Phase 1+2: screen_directional.py discovers and deep-screens wallets
# Phase 3a:  verify_passers.py --tier1-only runs after each batch (fast)
#            kills negative all-time P&L immediately
# Phase 3b:  verify_passers.py full Tier 2 deep dive at the end
#            finds Priority Shadow candidates from all Tier 1 survivors
#
# Usage:
#   ./scripts/run_pipeline.sh              # fast mode, all batches (0-840)
#   ./scripts/run_pipeline.sh 60           # fast mode, start from batch 60
#   ./scripts/run_pipeline.sh 0 180        # fast mode, batches 0, 60, 120 only
#   ./scripts/run_pipeline.sh --deep       # DEEP mode: discovers ALL ~2,100
#                                           # active markets (vs top 120),
#                                           # defaults END to 24600 to cover
#                                           # the much larger resulting
#                                           # wallet pool (~24,500 human-size
#                                           # wallets vs ~1,135 in fast mode).
#                                           # Takes hours, not minutes — run
#                                           # deliberately, not via the
#                                           # automated cron job.
#   ./scripts/run_pipeline.sh --deep 0 5000  # deep mode, custom END override

set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
SCREEN="scripts/screen_directional.py"
VERIFY="scripts/verify_passers.py"
OUT_DIR="${PM_SCREEN_DIR:-/tmp/screen-v3}"
LOG="$OUT_DIR/pipeline.log"
PASSERS="$OUT_DIR/passers.json"
ALL_VERIFIED="$OUT_DIR/all_verified_passers.json"

# Parse --deep flag from anywhere in the arguments, leaving the remaining
# positional args (START, END) intact regardless of where --deep appears.
DEEP_FLAG=""
ARGS=()
for arg in "$@"; do
  if [ "$arg" = "--deep" ]; then
    DEEP_FLAG="--deep"
  else
    ARGS+=("$arg")
  fi
done

START="${ARGS[0]:-0}"
if [ -n "$DEEP_FLAG" ]; then
  # Deep mode discovers far more wallets (~24,500 human-size as of testing
  # vs ~1,135 in fast mode) — the old END=840 default only covers a small
  # fraction of that pool. Default to a very high ceiling (99999) so deep
  # mode always screens the ENTIRE discovered pool regardless of how large
  # it grows over time, rather than needing this number manually updated —
  # screen_directional.py naturally stops once it runs out of wallets at a
  # given offset, so a high ceiling costs nothing if the real pool is
  # smaller. Override explicitly as the second positional argument if a
  # smaller deliberate ceiling is ever wanted.
  END="${ARGS[1]:-9999999}"
else
  END="${ARGS[1]:-840}"
fi
STEP=60

mkdir -p "$OUT_DIR"

echo "========================================" | tee -a "$LOG"
echo "Pipeline started: $(date -u '+%Y-%m-%dT%H:%M:%SZ')" | tee -a "$LOG"
echo "Batches: $START to $END (step $STEP)" | tee -a "$LOG"
echo "========================================" | tee -a "$LOG"

if [ ! -f "$ALL_VERIFIED" ]; then
  echo "[]" > "$ALL_VERIFIED"
fi

TOTAL_SCREEN_PASSERS=0

off=$START
while [ "$off" -le "$END" ]; do
  echo "" | tee -a "$LOG"
  echo "===== BATCH $off =====" | tee -a "$LOG"

  $PYTHON "$SCREEN" "$off" $DEEP_FLAG 2>&1 | tee -a "$LOG"

  # Stop early once we've run past the end of the discovered candidate pool
  # (the script prints "screening 0 candidates" via Python list slicing
  # naturally returning empty past the list end — see screen_directional.py
  # cands[args.offset:args.offset+n_deep]). Without this check, a high
  # default END (e.g. 99999 for --deep mode) would otherwise keep looping
  # uselessly all the way to that ceiling after the real pool is exhausted.
  if tail -5 "$LOG" | grep -q "screening 0 candidates"; then
    echo "[pipeline] 0 candidates in this batch — reached end of discovered pool, stopping early." | tee -a "$LOG"
    break
  fi

  PASSER_COUNT=0
  if [ -f "$PASSERS" ]; then
    PASSER_COUNT=$($PYTHON -c "import json; print(len(json.load(open('$PASSERS'))))" 2>/dev/null || echo 0)
  fi

  if [ "$PASSER_COUNT" -gt 0 ]; then
    echo "" | tee -a "$LOG"
    echo "--- $PASSER_COUNT screen passer(s) — Tier 1 quick check ---" | tee -a "$LOG"

    $PYTHON "$VERIFY" --tier1-only 2>&1 | tee -a "$LOG"

    $PYTHON -c "
import json, os
from pathlib import Path

out_dir = os.environ.get('PM_SCREEN_DIR', '/tmp/screen-v3')
all_path = Path(out_dir) / 'all_verified_passers.json'
new_path = Path(out_dir) / 'verified_passers.json'

existing = json.loads(all_path.read_text()) if all_path.exists() else []
new = json.loads(new_path.read_text()) if new_path.exists() else []

seen = {r['address'] for r in existing}
t1_survivors = [r for r in new if r.get('tier1_pass') is True]
added = [r for r in t1_survivors if r['address'] not in seen]
merged = existing + added
all_path.write_text(json.dumps(merged, indent=2))
if added:
    print(f'[pipeline] +{len(added)} Tier 1 survivor(s) -> {len(merged)} total in all_verified_passers.json')
else:
    print(f'[pipeline] 0 new Tier 1 survivors this batch (total: {len(merged)})')
" 2>/dev/null | tee -a "$LOG"

    TOTAL_SCREEN_PASSERS=$((TOTAL_SCREEN_PASSERS + PASSER_COUNT))
  fi

  off=$((off + STEP))
done

TIER1_COUNT=$($PYTHON -c "import json; d=json.load(open('$ALL_VERIFIED')); print(len(d))" 2>/dev/null || echo 0)

echo "" | tee -a "$LOG"
echo "========================================" | tee -a "$LOG"
echo "PHASE 3b — TIER 2 DEEP DIVE" | tee -a "$LOG"
echo "$TIER1_COUNT Tier 1 survivor(s) to deep-dive" | tee -a "$LOG"
echo "========================================" | tee -a "$LOG"

if [ "$TIER1_COUNT" -gt 0 ]; then
  ADDRESSES=$($PYTHON -c "import json; d=json.load(open('$ALL_VERIFIED')); print(','.join(r['address'] for r in d))" 2>/dev/null || echo "")

  if [ -n "$ADDRESSES" ]; then
    # Build a passers.json with win rates from screened_wallets.json
    SCREENED="$OUT_DIR/screened_wallets.json"
    TIER2_PASSERS="$OUT_DIR/tier2_passers.json"
    if [ -f "$SCREENED" ]; then
      $PYTHON -c "
import json
screened = json.loads(open('$SCREENED').read())
verified = json.loads(open('$ALL_VERIFIED').read())
verified_addrs = {r['address'].lower() for r in verified}
result = []
for addr, data in screened.items():
    if addr.lower() in verified_addrs:
        m = data.get('metrics', {})
        result.append({'address': addr, 'win_rate': m.get('win_rate'), 'closed': m.get('closed',0), 'closed_pnl': m.get('closed_pnl',0)})
open('$TIER2_PASSERS','w').write(json.dumps(result))
print(f'Built tier2_passers.json with {len(result)} wallets')
" 2>&1 | tee -a "$LOG"
      $PYTHON "$VERIFY" --passers "$TIER2_PASSERS" 2>&1 | tee -a "$LOG"
    else
      $PYTHON "$VERIFY" --addresses "$ADDRESSES" 2>&1 | tee -a "$LOG"
    fi

    # Merge the freshly-computed Tier 2 results back into
    # all_verified_passers.json by address. Without this, the final Tier 2
    # deep-dive only ever lands in verified_passers.json (the single-batch
    # working file, overwritten every batch) — leaving every entry in the
    # CUMULATIVE all_verified_passers.json permanently stuck at
    # tier2: null, even for wallets that genuinely passed. Confirmed this
    # exact bug Jun 24 — the Priority Shadow summary printed correctly to
    # the log, but DashView (which reads all_verified_passers.json) showed
    # tier2: null for every wallet, including ones known to have passed.
    $PYTHON -c "
import json, os
from pathlib import Path

out_dir = os.environ.get('PM_SCREEN_DIR', '/tmp/screen-v3')
all_path = Path(out_dir) / 'all_verified_passers.json'
tier2_results_path = Path(out_dir) / 'verified_passers.json'

all_data = json.loads(all_path.read_text()) if all_path.exists() else []
tier2_data = json.loads(tier2_results_path.read_text()) if tier2_results_path.exists() else []

tier2_by_addr = {r['address'].lower(): r.get('tier2') for r in tier2_data if r.get('address')}

updated = 0
for entry in all_data:
    addr = entry.get('address', '').lower()
    if addr in tier2_by_addr and tier2_by_addr[addr] is not None:
        entry['tier2'] = tier2_by_addr[addr]
        updated += 1

all_path.write_text(json.dumps(all_data, indent=2))
print(f'[pipeline] Merged Tier 2 results into all_verified_passers.json: {updated} wallet(s) updated')
" 2>&1 | tee -a "$LOG"
  fi
else
  echo "No Tier 1 survivors to deep-dive." | tee -a "$LOG"
fi

echo "" | tee -a "$LOG"
echo "========================================" | tee -a "$LOG"
echo "Pipeline complete: $(date -u '+%Y-%m-%dT%H:%M:%SZ')" | tee -a "$LOG"
echo "Screen passers: $TOTAL_SCREEN_PASSERS" | tee -a "$LOG"
echo "Tier 1 survivors: $TIER1_COUNT" | tee -a "$LOG"
echo "========================================" | tee -a "$LOG"
echo "Full log: $LOG"
echo "Results:  $OUT_DIR/verified_passers.json"
