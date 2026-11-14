#!/usr/bin/env bash
# Accuracy and speed of the int8 coarse path against the exact scan, measured in one binary.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
echo "== accuracy against the gate-proving binary"
python3 -u tests/probe_f16_recall.py --binary "$BIN" --golden /mnt/e/pw/round2/golden/mvdb-gate-proven \
  --queries 300 2>&1 | tail -2
echo "== speed (interleaved, 2 workers)"
for round in 1 2 3; do
  for mode in 0 1; do
    dir=$(mktemp -d)
    printf 'round %s coarse=%s  ' "$round" "$mode"
    MVDB_COARSE=$mode MVDB_SCAN_WORKERS=2 MVDB_SELFBENCH=400 "$BIN" --data-dir "$dir" 2>&1 \
      | sed -n 's/^\[selfbench\] //p' | head -1
    rm -rf "$dir"
  done
done
