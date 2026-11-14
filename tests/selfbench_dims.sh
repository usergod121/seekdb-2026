#!/usr/bin/env bash
# Same row count, different dimensionality: the scan cost per element tells whether the scan is limited by
# bytes read (bandwidth) or by arithmetic (compute).
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
WORKERS=${WORKERS:-1}
for dims in 128 64 32 16; do
  dir=$(mktemp -d)
  printf 'dims=%-4s workers=%s  ' "$dims" "$WORKERS"
  MVDB_SCAN_WORKERS=$WORKERS MVDB_SELFBENCH_DIMS=$dims MVDB_SELFBENCH=400 "$BIN" --data-dir "$dir" 2>&1 \
    | sed -n 's/^\[selfbench\] //p' | head -1
  rm -rf "$dir"
done
