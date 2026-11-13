#!/usr/bin/env bash
# One-line-per-metric summary of the release candidate, for the status report.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
echo "== query latency (1000 queries, half filtered)"
python3 -u tests/bench_query_min.py --binary "$BIN" --filtered half
echo "== write phase"
python3 -u tests/bench_write_cost.py
echo "== write split (flush vs own work)"
python3 -u tests/bench_write_split.py --batches 1,100 --repeat 2 | tail -2
echo "== i/o and restart"
python3 -u tests/probe_io_budget.py | head -1
echo "== cache budget knee"
bash tests/bench_cache_budget.sh | grep -E 'budget|min_us' | head -8
