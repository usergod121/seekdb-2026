#!/usr/bin/env bash
# A/B differential across a range of seeds: the golden binary and the candidate must agree on every
# reply. Usage: ab_seeds.sh <seed_from> <seed_to> <steps>
set -u
cd /mnt/e/mvdb || exit 1
GOLDEN=/mnt/e/pw/round2/golden/mvdb-golden-29c93ce
CANDIDATE=${MVDB_BIN:-/mnt/e/mvdb/build/minivectordb}
from=${1:-1}
to=${2:-4}
steps=${3:-120}
bad=0
for seed in $(seq "$from" "$to"); do
  out=$(timeout 900 python3 -u tests/ab_differential.py --golden "$GOLDEN" \
        --candidate "$CANDIDATE" --seed "$seed" --steps "$steps" 2>&1)
  code=$?
  summary=$(printf '%s\n' "$out" | grep -E '^seed ' | tail -1)
  echo "${summary:-seed $seed: crashed}"
  if [ "$code" -ne 0 ]; then
    printf '%s\n' "$out" | grep -E 'DIVERGENCE|Error' | head -4
    bad=$((bad + 1))
  fi
done
echo "seeds with divergence: $bad"
