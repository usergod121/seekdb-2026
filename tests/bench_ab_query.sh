#!/usr/bin/env bash
# Compares per-query latency between the golden binary and the candidate, twice each, low-noise.
set -u
cd /mnt/e/mvdb || exit 1
export TMPDIR=/tmp/mvdb-native
GOLDEN=${MVDB_GOLDEN:-/mnt/e/pw/round2/golden/mvdb-golden-current}
CANDIDATE=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
for round in 1 2; do
  python3 tests/bench_query_min.py --binary "$GOLDEN" --filtered none
  python3 tests/bench_query_min.py --binary "$CANDIDATE" --filtered none
done
for round in 1 2; do
  python3 tests/bench_query_min.py --binary "$GOLDEN" --filtered half
  python3 tests/bench_query_min.py --binary "$CANDIDATE" --filtered half
done
