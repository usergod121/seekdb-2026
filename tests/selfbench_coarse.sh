#!/usr/bin/env bash
# In-process A/B of the int8 coarse pass: exact scan (MVDB_COARSE=0) against coarse+rerank (=1), alternating.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
QUERIES=${QUERIES:-400}
ROUNDS=${ROUNDS:-3}
for round in $(seq 1 "$ROUNDS"); do
  for coarse in 0 1; do
    dir=$(mktemp -d)
    printf 'round %s coarse=%s  ' "$round" "$coarse"
    MVDB_COARSE=$coarse MVDB_SELFBENCH=$QUERIES "$BIN" --data-dir "$dir" 2>&1 | sed -n 's/^\[selfbench\] //p' | tr '\n' ' '
    echo
    rm -rf "$dir"
  done
done
