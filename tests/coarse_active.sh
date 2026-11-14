#!/usr/bin/env bash
# Confirms whether the coarse branch handles the unfiltered queries: that branch returns before the
# per-query timing print, so the number of prints drops when it is active.
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
for coarse in 0 1; do
  dir=$(mktemp -d)
  count=$(MVDB_QTIME=1 MVDB_COARSE=$coarse MVDB_SCAN_WORKERS=2 MVDB_SELFBENCH=20 "$BIN" --data-dir "$dir" 2>&1 \
    | grep -c '\[qtime\]' || true)
  echo "coarse=$coarse  qtime-prints=$count"
  rm -rf "$dir"
done
