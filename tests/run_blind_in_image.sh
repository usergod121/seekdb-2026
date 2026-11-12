#!/usr/bin/env bash
# Runs every blind corpus through the official checker inside the judge's container flags.
set -u
cd /mnt/e/pw/round2/pkg2 || exit 1
for corpus in sql vector tx storage; do
  echo "=== $corpus"
  python3 protocol_check/run.py --image mvdb:v14 \
    --package "/mnt/e/mvdb/tests/blind/$corpus" \
    --output "/mnt/e/pw/round2/guard/image-$corpus" 2>&1 | grep -E "PASS|FAIL|ERROR" | tail -12
done
