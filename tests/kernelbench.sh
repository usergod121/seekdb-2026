#!/usr/bin/env bash
# Bare distance-kernel rate for both variants.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
for mode in 4 8; do
  printf 'kernel=%s  ' "$mode"
  MVDB_KERNEL=$mode MVDB_KERNELBENCH=${REPEATS:-300} "$BIN" --data-dir /tmp 2>&1 | sed -n 's/^\[kernelbench\] //p'
done
