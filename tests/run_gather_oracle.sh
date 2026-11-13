#!/usr/bin/env bash
# Runs the gather-path oracle differential over several seeds, against the candidate and against the
# golden binary (which has no equality-group path, so it also validates the oracle itself).
set -u
cd /mnt/e/mvdb || exit 1
GOLDEN=${MVDB_GOLDEN:-/mnt/e/pw/round2/golden/mvdb-golden-current}
CANDIDATE=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
for seed in 1 2 3 4 5; do
  printf 'candidate seed %s: ' "$seed"
  python3 -u tests/probe_gather_oracle.py --binary "$CANDIDATE" --seed "$seed" --queries 300 2>&1 | tail -1
done
printf 'golden    seed 9: '
python3 -u tests/probe_gather_oracle.py --binary "$GOLDEN" --seed 9 --queries 200 2>&1 | tail -1
