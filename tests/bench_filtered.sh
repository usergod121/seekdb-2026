#!/usr/bin/env bash
# Filtered-only query latency, three runs, for the gather-path comparison.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${1:-/mnt/e/mvdb/build/minivectordb}
FILTER=${2:-all}
for i in 1 2 3; do
  python3 -u tests/bench_query_min.py --binary "$BIN" --filtered "$FILTER" | \
    python3 -c "import json,sys;d=json.load(sys.stdin);print('min %.2f  p10 %.2f  median %.2f' % (d['min_us'],d['p10_us'],d['median_us']))"
done
