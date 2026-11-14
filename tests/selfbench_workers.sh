#!/usr/bin/env bash
# How does the scan scale with the worker count? If 1 worker and 12 workers measure the same, the scan is
# not being split and the pool round-trip is what a query pays.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
QUERIES=${QUERIES:-400}
for workers in 1 2 4 8; do
  dir=$(mktemp -d)
  printf 'workers=%-2s  ' "$workers"
  MVDB_SCAN_WORKERS=$workers MVDB_SELFBENCH=$QUERIES "$BIN" --data-dir "$dir" 2>&1 | sed -n 's/^\[selfbench\] //p' | tr '\n' ' '
  echo
  rm -rf "$dir"
done
