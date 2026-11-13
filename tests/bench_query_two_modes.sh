#!/usr/bin/env bash
# Filtered-only and mixed query latency for one binary.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${1:-/mnt/e/mvdb/build/minivectordb}
echo "== filtered only"
for i in 1 2 3; do
  python3 -u tests/bench_query_min.py --binary "$BIN" --filtered all | \
    python3 -c "import json,sys;d=json.load(sys.stdin);print('  min %.2f  p10 %.2f  median %.2f' % (d['min_us'],d['p10_us'],d['median_us']))"
done
echo "== mixed (half filtered)"
for i in 1 2 3; do
  python3 -u tests/bench_query_min.py --binary "$BIN" --filtered half | \
    python3 -c "import json,sys;d=json.load(sys.stdin);print('  min %.2f  p10 %.2f  median %.2f' % (d['min_us'],d['p10_us'],d['median_us']))"
done
