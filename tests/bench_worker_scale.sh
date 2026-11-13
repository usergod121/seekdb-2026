#!/usr/bin/env bash
# Records how the query time scales with the scan pool size, and how much the same binary moves between
# identical runs. Together these say whether the kernel is still worth optimising on this machine.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
for workers in 1 2 3 4; do
  echo "== MVDB_SCAN_WORKERS=$workers"
  MVDB_SCAN_WORKERS=$workers python3 -u tests/bench_query_min.py --binary "$BIN" --filtered half
done
echo "== repeat runs with the default pool size"
for round in 1 2 3; do
  python3 -u tests/bench_query_min.py --binary "$BIN" --filtered half
done
