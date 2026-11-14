#!/usr/bin/env bash
# Runs a small self bench with per-query phase timings enabled and prints the median split.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
dir=$(mktemp -d)
MVDB_QTIME=1 MVDB_SCAN_WORKERS="${WORKERS:-2}" MVDB_SELFBENCH="${QUERIES:-60}" \
  MVDB_SELFBENCH_DIMS="${DIMS:-128}" "$BIN" --data-dir "$dir" 2>&1 | grep '\[qtime\]' | head -6
rm -rf "$dir"
