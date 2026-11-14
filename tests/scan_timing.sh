#!/usr/bin/env bash
# Prints the inside of a scan: how much of a query is the distance kernel, the threshold filter, and the
# TopK visits.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
dir=$(mktemp -d)
MVDB_QTIME=1 MVDB_SCAN_WORKERS="${WORKERS:-1}" MVDB_SELFBENCH="${QUERIES:-40}" \
  MVDB_SELFBENCH_DIMS="${DIMS:-128}" "$BIN" --data-dir "$dir" 2>&1 | grep -E '\[scan\]|\[qtime\]' | head -8
rm -rf "$dir"
