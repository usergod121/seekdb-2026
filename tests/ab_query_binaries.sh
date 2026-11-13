#!/usr/bin/env bash
# Interleaved comparison of one query mode between two binaries; the VM drifts, so alternating runs is
# the only way to compare small differences.
set -u
cd /mnt/e/mvdb || exit 1
A=${1:-/mnt/e/pw/round2/golden/mvdb-gate-proven}
B=${2:-/mnt/e/mvdb/build/minivectordb}
MODE=${3:-none}
ROUNDS=${4:-2}
for round in $(seq 1 "$ROUNDS"); do
  for bin in "$A" "$B"; do
    printf '%-24s ' "$(echo "$bin" | sed 's|.*/\([^/]*\)/\([^/]*\)$|\1/\2|')"
    python3 -u tests/bench_query_min.py --binary "$bin" --filtered "$MODE" | \
      python3 -c "import json,sys;d=json.load(sys.stdin);print('min %.2f  p10 %.2f  median %.2f  mean %.2f' % (d['min_us'],d['p10_us'],d['median_us'],d['mean_us']))"
  done
done
