#!/usr/bin/env bash
# Compares query latency between two builds, three runs each, minimum of the minima.
set -u
cd /mnt/e/mvdb || exit 1
A=${1:-/mnt/e/pw/round2/wt-profile/build/minivectordb}
B=${2:-/mnt/e/mvdb/build/minivectordb}
FILTER=${3:-all}
for bin in "$A" "$B"; do
  printf '== %s\n' "$bin"
  for i in 1 2 3; do
    python3 -u tests/bench_query_min.py --binary "$bin" --filtered "$FILTER" | \
      sed -e 's/.*"min_us": \([0-9.]*\).*/min \1/' -e 's/.*"p10_us": \([0-9.]*\)/"p10 \1"/' > /dev/null 2>&1
    python3 -u tests/bench_query_min.py --binary "$bin" --filtered "$FILTER" | \
      python3 -c "import json,sys;d=json.load(sys.stdin);print('  min %.2f  p10 %.2f  median %.2f  mean %.2f' % (d['min_us'],d['p10_us'],d['median_us'],d['mean_us']))"
  done
done
