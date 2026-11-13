#!/usr/bin/env bash
# Interleaved A/B of filtered-query latency between two builds, to beat the VM's drift.
set -u
cd /mnt/e/mvdb || exit 1
A=${1:?first binary}
B=${2:?second binary}
for round in 1 2 3; do
  for bin in "$A" "$B"; do
    printf '%-28s ' "$(basename "$(dirname "$(dirname "$bin")")")/$(basename "$bin")"
    python3 -u tests/bench_query_min.py --binary "$bin" --filtered all | \
      python3 -c "import json,sys;d=json.load(sys.stdin);print('min %.2f  p10 %.2f  median %.2f' % (d['min_us'],d['p10_us'],d['median_us']))"
  done
done
