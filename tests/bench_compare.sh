#!/usr/bin/env bash
# Compares benchmark numbers across pool sizes.
set -u
cd /mnt/e/mvdb || exit 1
export TMPDIR=/tmp/mvdb-native
run() {
  local label="$1"
  local pool="$2"
  local out
  if [ "$pool" = "default" ]; then
    out=$(python3 tests/bench_round2.py)
  else
    out=$(MVDB_POOL_PAGES="$pool" python3 tests/bench_round2.py)
  fi
  echo "$label $out" | cut -c1-190
}
run "pool=default r1" default
run "pool=default r2" default
run "pool=4096 r1" 4096
run "pool=4096 r2" 4096
run "pool=128 r1" 128
run "pool=128 r2" 128
