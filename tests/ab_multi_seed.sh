#!/usr/bin/env bash
# Multi-seed differential against the gate-proving binary: the strongest local evidence that today's
# engine changes answer exactly like the version the platform accepted.
set -u
cd /mnt/e/mvdb || exit 1
GOLDEN=${MVDB_GOLDEN:-/mnt/e/pw/round2/golden/mvdb-gate-proven}
CANDIDATE=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
fail=0
for seed in 1 2 3 4 5 6 7 8; do
  printf 'ab seed %s: ' "$seed"
  out=$(python3 -u tests/ab_differential.py --golden "$GOLDEN" --candidate "$CANDIDATE" \
        --seed "$seed" --steps 2500 2>&1 | tail -1)
  echo "$out"
  case "$out" in
    *"0 divergence(s)"*) ;;
    *) fail=$((fail + 1)) ;;
  esac
done
echo "seeds with divergence: $fail"
