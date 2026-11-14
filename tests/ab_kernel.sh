#!/usr/bin/env bash
# Interleaved A/B of the two scan kernels inside one binary, alternating rounds.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
WORKERS=${WORKERS:-2}
for round in 1 2 3; do
  for mode in 4 8; do
    dir=$(mktemp -d)
    printf 'round %s kernel=%s  ' "$round" "$mode"
    MVDB_KERNEL=$mode MVDB_SCAN_WORKERS=$WORKERS MVDB_SELFBENCH=400 "$BIN" --data-dir "$dir" 2>&1 \
      | sed -n 's/^\[selfbench\] //p' | head -1
    rm -rf "$dir"
  done
done
