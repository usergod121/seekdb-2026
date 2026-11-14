#!/usr/bin/env bash
# One profiled self-bench run: prints the per-stage profile of a query session.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
dir=$(mktemp -d)
MVDB_PROFILE=1 MVDB_SCAN_WORKERS="${WORKERS:-2}" MVDB_SELFBENCH="${QUERIES:-200}" \
  MVDB_SELFBENCH_DIMS="${DIMS:-128}" "$BIN" --data-dir "$dir" 2>&1 | grep -E 'selfbench|profile'
rm -rf "$dir"
