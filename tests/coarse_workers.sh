#!/usr/bin/env bash
# Coarse-path latency at several worker counts.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
for workers in 1 2 4; do
  dir=$(mktemp -d)
  printf 'workers=%-2s ' "$workers"
  MVDB_SCAN_WORKERS=$workers MVDB_SELFBENCH=400 "$BIN" --data-dir "$dir" 2>&1 \
    | sed -n 's/^\[selfbench\] \(unfiltered\|filtered\).*min *\([0-9.]*\) us.*median *\([0-9.]*\) us.*/\1 min \2 median \3/p' \
    | tr '\n' ' '
  echo
  rm -rf "$dir"
done
