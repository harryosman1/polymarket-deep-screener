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
#   ./scripts/run_pipeline.sh              # all batches (0-840)
#   ./scripts/run_pipeline.sh 60           # start from batch 60
#   ./scripts/run_pipeline.sh 0 180        # batches 0, 60, 120 only

set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
SCREEN="scripts/screen_directional.py"
VERIFY="scripts/verify_passers.py"
OUT_DIR="${PM_SCREEN_DIR:-/tmp/screen-v3}"
LOG="$OUT_DIR/pipeline.log"
PASSERS="$OUT_DIR/passers.json"
ALL_VERIFIED="$OUT_DIR/all_verified_passers.json"

START="${1:-0}"
END="${2:-840}"
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

  $PYTHON "$SCREEN" "$off" 2>&1 | tee -a "$LOG"

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
    $PYTHON "$VERIFY" --addresses "$ADDRESSES" 2>&1 | tee -a "$LOG"
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
