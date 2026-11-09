#!/usr/bin/env bash
# Compares the pooled scan at several worker counts.
set -u
cd /mnt/e/mvdb || exit 1
for w in 1 2 4 8; do
  echo "== workers=$w"
  MVDB_SCAN_WORKERS=$w python3 tests/cost_breakdown.py 2>&1 | grep -E "unfiltered|filtered"
done
