#!/usr/bin/env bash
# Runs every blind (spec-derived) corpus under tests/blind/ and prints one line per corpus,
# followed by the details of each disagreeing statement.
set -u
cd /mnt/e/mvdb || exit 1
total_fail=0
for corpus in tests/blind/*/; do
  [ -f "$corpus/manifest.json" ] || continue
  name=$(basename "$corpus")
  printf '=== %-12s ' "$name"
  out=$(MVDB_CASES="/mnt/e/mvdb/$corpus" timeout 1800 python3 -u tests/run_official_cases.py 2>&1)
  summary=$(printf '%s\n' "$out" | tail -1)
  echo "$summary"
  if printf '%s\n' "$out" | grep -qE 'FAIL'; then
    printf '%s\n' "$out" | grep -E 'FAIL|^  ' | head -40
    total_fail=$((total_fail + 1))
  fi
done
echo "corpora with disagreements: $total_fail"
