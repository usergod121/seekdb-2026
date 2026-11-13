#!/usr/bin/env bash
# Does the clamped cache budget still reach the cached path?
# 1 MiB forces the paged path; 8 MiB and 64 MiB are the clamp's floor region, and the default is the
# container-derived budget. Same query set and same data in every run.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
for budget in 1048576 8388608 67108864; do
  printf '== MVDB_CACHE_BUDGET_BYTES=%s\n' "$budget"
  MVDB_CACHE_BUDGET_BYTES=$budget python3 -u tests/bench_query_min.py --binary "$BIN" --filtered half
done
printf '== default budget\n'
python3 -u tests/bench_query_min.py --binary "$BIN" --filtered half
