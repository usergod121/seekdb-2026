#!/usr/bin/env bash
# Durability-critical suites: the close path now keeps the log instead of rewriting the pages.
set -u
cd /mnt/e/mvdb || exit 1
for t in probe_crash_loop probe_scale_durability probe_wal_batch probe_scale_model probe_tx_matrix \
         probe_bulk probe_table_shapes probe_wide_rows probe_crash_shapes probe_matrix_writes \
         probe_matrix probe_order_matrix probe_reject_all probe_gather_oracle probe_io_budget; do
  printf '%-24s ' "$t"
  python3 -u "tests/$t.py" 2>&1 | tail -1
done
printf '%-24s ' 'ab_differential'
python3 -u tests/ab_differential.py --golden /mnt/e/pw/round2/golden/mvdb-golden-current \
  --candidate build/minivectordb --steps 2000 2>&1 | tail -1
printf '%-24s ' 'official_cases'
python3 -u tests/run_official_cases.py 2>&1 | tail -1
