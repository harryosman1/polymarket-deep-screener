#!/usr/bin/env bash
# run_pipeline.sh — Run the full screen + verify pipeline in one shot.
#
# For each batch:
#   1. Runs screen_directional.py (phase 1 + 2)
#   2. If any passers found, immediately runs verify_passers.py (phase 3)
#
# Usage:
#   ./scripts/run_pipeline.sh              # all batches (0-840)
#   ./scripts/run_pipeline.sh 60           # start from batch 60
#   ./scripts/run_pipeline.sh 0 180        # batches 0, 60, 120 only
#
# Output:
#   All output logged to $PM_SCREEN_DIR/pipeline.log
#   Verified passers accumulate in $PM_SCREEN_DIR/all_verified_passers.json

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

[ -f "$ALL_VERIFIED" ] || echo "[]" > "$ALL_VERIFIED"

TOTAL_PASSERS=0

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
    echo "--- $PASSER_COUNT passer(s) found — verifying ---" | tee -a "$LOG"

    $PYTHON "$VERIFY" 2>&1 | tee -a "$LOG"

    $PYTHON - << 'PYEOF' 2>/dev/null
import json, os
from pathlib import Path

out_dir = os.environ.get("PM_SCREEN_DIR", "/tmp/screen-v3")
all_path = Path(out_dir) / "all_verified_passers.json"
new_path = Path(out_dir) / "verified_passers.json"

existing = json.loads(all_path.read_text()) if all_path.exists() else []
new = json.loads(new_path.read_text()) if new_path.exists() else []

seen = {r["address"] for r in existing}
added = [r for r in new if r["address"] not in seen]
merged = existing + added
all_path.write_text(json.dumps(merged, indent=2))
print(f"[pipeline] +{len(added)} verified → {len(merged)} total in all_verified_passers.json")
PYEOF

    TOTAL_PASSERS=$((TOTAL_PASSERS + PASSER_COUNT))
  fi

  off=$((off + STEP))
done

echo "" | tee -a "$LOG"
echo "========================================" | tee -a "$LOG"
echo "Pipeline complete: $(date -u '+%Y-%m-%dT%H:%M:%SZ')" | tee -a "$LOG"
echo "Total passers found: $TOTAL_PASSERS" | tee -a "$LOG"

$PYTHON - << 'PYEOF' 2>/dev/null
import json, os
from pathlib import Path

out_dir = os.environ.get("PM_SCREEN_DIR", "/tmp/screen-v3")
results = json.loads((Path(out_dir) / "all_verified_passers.json").read_text())
promising = [r for r in results if r.get("all_time_profit") and r["all_time_profit"] >= 500]
negative  = [r for r in results if r.get("all_time_profit") and r["all_time_profit"] < 0]

print(f"\n=== FINAL PIPELINE SUMMARY ===")
print(f"  Total verified:  {len(results)}")
print(f"  Promising:       {len(promising)}")
print(f"  Negative P&L:    {len(negative)}")
print(f"  Unclear/error:   {len(results) - len(promising) - len(negative)}")

if promising:
    print(f"\n=== WORTH SHADOWING ({len(promising)}) ===")
    for r in sorted(promising, key=lambda x: x["all_time_profit"], reverse=True):
        sd = r.get("screen_metrics", {})
        wr = sd.get("win_rate")
        print(f"  {r['address']}  "
              f"all_time=+${r['all_time_profit']:,.0f}  "
              f"screen_wr={f'{wr:.0%}' if wr else '—'}  "
              f"predictions={r.get('predictions') or '—'}")
PYEOF

echo "========================================"
echo "Full log: $LOG"
echo "All verified: $ALL_VERIFIED"
