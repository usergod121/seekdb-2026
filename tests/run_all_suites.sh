#!/usr/bin/env bash
# Runs every local suite and prints one line per suite.
set -u
cd /mnt/e/mvdb || exit 1
for f in boundary_suite.py boundary_suite2.py adversarial_protocol.py probe_syntax2.py probe_syntax3.py probe_syntax4.py probe_row_width.py probe_wal_batch.py; do
  printf '%-26s ' "$f"
  out=$(timeout 900 python3 -u "tests/$f" 2>/dev/null | grep -E 'passed|problem\(s\)' | tail -1)
  echo "${out:-no summary}"
done
echo -n "official protocol_check PASS count: "
cd /mnt/e/pw/round2/pkg2 && ./run_local_check.sh 2>/dev/null | grep -cE ': PASS'
