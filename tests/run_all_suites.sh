#!/usr/bin/env bash
# Runs every local suite and prints one line per suite.
set -u
cd /mnt/e/mvdb || exit 1
for f in boundary_suite.py boundary_suite2.py adversarial_protocol.py probe_syntax2.py probe_syntax3.py probe_syntax4.py probe_row_width.py probe_wal_batch.py probe_finite.py probe_schema.py probe_tx_matrix.py probe_scale_durability.py; do
  printf '%-26s ' "$f"
  out=$(timeout 1200 python3 -u "tests/$f" 2>/dev/null | grep -E 'passed|problem' | tail -1)
  echo "${out:-no summary}"
done
# independent re-implementation of the case comparison (95 statements, restarts and ann mode included)
printf '%-26s ' "run_official_cases.py"
MVDB_CASES=/mnt/e/pw/round2/pkg2/mysqltest timeout 900 python3 -u tests/run_official_cases.py 2>/dev/null | tail -1
echo -n "official protocol_check PASS count: "
cd /mnt/e/pw/round2/pkg2 && ./run_local_check.sh 2>/dev/null | grep -cE ': PASS'
