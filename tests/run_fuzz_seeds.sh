#!/usr/bin/env bash
# Runs the differential fuzzer over a range of seeds and stops on the first divergence.
set -u
seed_from=${1:-1}
seed_to=${2:-8}
statements=${3:-150}
cd /mnt/e/mvdb || exit 1
bad=0
for seed in $(seq "$seed_from" "$seed_to"); do
  out=$(timeout 900 python3 -u tests/fuzz_differential.py --seed "$seed" --statements "$statements" 2>&1)
  code=$?
  summary=$(printf '%s\n' "$out" | grep -E '^seed ' | tail -1)
  echo "${summary:-seed $seed: crashed}"
  if [ "$code" -ne 0 ]; then
    printf '%s\n' "$out" | grep -E 'DIVERGENCE|Error|Traceback' | head -8
    bad=$((bad + 1))
  fi
done
echo "seeds with divergence: $bad"
exit 0
