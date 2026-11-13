#!/usr/bin/env bash
# Sweeps the ANN probe count and reports recall@10 against the exact scan plus latency, which is the
# frontier the task's 0.95 floor is judged on.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
for probe in 8 16 24 32 48 64 96 156; do
  printf 'nprobe=%-4s ' "$probe"
  MVDB_ANN_PROBE=$probe python3 -u tests/probe_ann_recall.py --binary "$BIN" --queries 200 2>&1 | tr '\n' ' '
  echo
done
