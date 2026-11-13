#!/usr/bin/env bash
# Quick but broad regression check after a parser-only change: semantics first, timing second.
set -u
cd /mnt/e/mvdb || exit 1
for t in probe_matrix probe_order_matrix probe_matrix_writes probe_select_extras probe_mysql_syntax probe_like_between probe_reject_all probe_predicate_types probe_protocol_fuzz probe_gather_oracle; do
  printf '%-24s ' "$t"
  python3 -u "tests/$t.py" 2>&1 | tail -1
done
printf '%-24s ' 'ab_differential'
python3 -u tests/ab_differential.py --golden /mnt/e/pw/round2/golden/mvdb-golden-current \
  --candidate build/minivectordb --steps 2000 2>&1 | tail -1
printf '%-24s ' 'official_cases'
python3 -u tests/run_official_cases.py 2>&1 | tail -1
