#!/usr/bin/env bash
# Phase timings of the coarse branch (prescan / coarse scan / guard+rerank / emit).
set -u
cd /mnt/e/mvdb || exit 1
BIN=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
dir=$(mktemp -d)
MVDB_QTIME=1 MVDB_SCAN_WORKERS="${WORKERS:-2}" MVDB_SELFBENCH="${QUERIES:-40}" \
  "$BIN" --data-dir "$dir" 2>&1 | grep -E '^\[coarse\]|^\[qtime\]' | head -8
rm -rf "$dir"
