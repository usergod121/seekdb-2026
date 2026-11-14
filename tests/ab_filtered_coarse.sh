#!/usr/bin/env bash
# Accuracy and speed of the filtered coarse path against the exact gather path.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
echo "== accuracy: filtered answers against the gate-proving binary"
python3 -u tests/probe_f16_recall.py --binary "$BIN" --golden /mnt/e/pw/round2/golden/mvdb-gate-proven \
  --queries 300 2>&1 | tail -1
echo "== ties / churn"
python3 -u tests/probe_coarse_ties.py --binary "$BIN" 2>&1 | tail -1
echo "== speed (interleaved, 2 workers, filtered half)"
for round in 1 2 3; do
  for mode in 0 1; do
    dir=$(mktemp -d)
    printf 'round %s coarse=%s  ' "$round" "$mode"
    MVDB_COARSE=$mode MVDB_SCAN_WORKERS=2 MVDB_SELFBENCH=400 "$BIN" --data-dir "$dir" 2>&1 \
      | sed -n 's/^\[selfbench\] filtered.*min *\([0-9.]*\) us.*median *\([0-9.]*\) us.*/filtered min \1 median \2/p'
    rm -rf "$dir"
  done
done
