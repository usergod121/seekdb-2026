#!/bin/bash
# Rough shape of the graded workload, for development timing only (not the official driver):
#   N rows with 128 dimension vectors loaded in batches of 100 inside explicit transactions,
#   then M TopK queries, half of them filtered.
set -u
BIN=${BIN:-build/minivectordb}
DIR=${1:-/tmp/mvdb_perf}
ROWS=${ROWS:-10000}
QUERIES=${QUERIES:-1000}
SQL=/tmp/mvdb_perf.sql
rm -rf "$DIR"
if [ ! -s "$SQL" ]; then
  python3 tests/perf_gen.py "$ROWS" "$QUERIES" > "$SQL"
fi
echo "sql statements: $(wc -l < "$SQL")"

# load phase
awk '/^SELECT/{exit} {print}' "$SQL" > /tmp/mvdb_perf_load.sql
awk '/^SELECT/{print}' "$SQL" > /tmp/mvdb_perf_query.sql

START=$(date +%s%N)
"$BIN" --data-dir "$DIR" < /tmp/mvdb_perf_load.sql > /tmp/mvdb_perf_load.out 2>/tmp/mvdb_perf_load.err
END=$(date +%s%N)
LOAD_MS=$(( (END - START) / 1000000 ))
echo "load: ${LOAD_MS} ms  (errors: $(grep -c ERROR /tmp/mvdb_perf_load.out || true))"

START=$(date +%s%N)
"$BIN" --data-dir "$DIR" < /tmp/mvdb_perf_query.sql > /tmp/mvdb_perf_query.out 2>/tmp/mvdb_perf_query.err
END=$(date +%s%N)
QUERY_MS=$(( (END - START) / 1000000 ))
echo "query: ${QUERY_MS} ms for ${QUERIES} queries  (errors: $(grep -c ERROR /tmp/mvdb_perf_query.out || true))"
echo "per query: $(( QUERY_MS * 1000 / QUERIES )) us"
echo "data dir:"; ls -l "$DIR" | tail -5
echo "first result block:"; head -13 /tmp/mvdb_perf_query.out
