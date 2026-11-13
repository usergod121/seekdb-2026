#!/usr/bin/env bash
# Compares the strict commit policy with the grouped one, for both phases, inside one binary.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
echo "== write phase (10000 rows, 100 batches)"
for policy in strict grouped; do
  dir=$(mktemp -d)
  started=$(date +%s%N)
  MVDB_SYNC_POLICY=$policy python3 -u tests/bench_write_cost.py 2>&1 | sed -n 's/^\(A .*\)$/  '"$policy"': \1/p' | head -2
  rm -rf "$dir"
done
echo "== in-process query phase (400 queries, half filtered)"
for policy in strict grouped; do
  dir=$(mktemp -d)
  printf '  %-8s ' "$policy"
  MVDB_SYNC_POLICY=$policy MVDB_SELFBENCH=400 "$BIN" --data-dir "$dir" 2>&1 | sed -n 's/^\[selfbench\] //p' | tr '\n' ' '
  echo
  rm -rf "$dir"
done
