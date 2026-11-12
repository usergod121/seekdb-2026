#!/usr/bin/env bash
# Runs every local suite and prints one line per suite.
set -u
cd /mnt/e/mvdb || exit 1
for f in boundary_suite.py boundary_suite2.py adversarial_protocol.py probe_syntax2.py probe_syntax3.py probe_syntax4.py probe_row_width.py probe_wal_batch.py probe_finite.py probe_schema.py probe_tx_matrix.py probe_scale_durability.py probe_crash_loop.py probe_reject_all.py probe_wide_rows.py probe_bulk.py probe_table_shapes.py probe_select_extras.py probe_extreme_vectors.py probe_like_between.py probe_mysql_syntax.py probe_crash_shapes.py probe_sigpipe.py probe_container_memory.py probe_predicate_types.py probe_matrix.py probe_matrix_writes.py probe_order_matrix.py; do
  printf '%-26s ' "$f"
  out=$(timeout 1200 python3 -u "tests/$f" 2>/dev/null | grep -E 'passed|problem|failures' | tail -1)
  echo "${out:-no summary}"
done
# blind corpora: cases designed from the task statement alone, without looking at the engine
printf '%-26s ' "blind corpora"
out=$(bash tests/run_blind_corpora.sh 2>/dev/null | tail -1)
echo "${out:-no summary}"
# A/B: the same random workload on the golden binary and on this build must answer identically
printf '%-26s ' "ab_differential (4 seeds)"
out=$(bash tests/ab_seeds.sh 1 4 100 2>/dev/null | tail -1)
echo "${out:-no summary}"
# randomised differential testing against an independent model of the spec
printf '%-26s ' "fuzz_differential (6 seeds)"
out=$(bash tests/run_fuzz_seeds.sh 1 6 120 2>/dev/null | tail -1)
echo "${out:-no summary}"
# harder siblings of the public cases, expectations computed by an independent Python model
printf '%-26s ' "hard_cases (modelled)"
timeout 300 python3 -u tests/gen_hard_cases.py >/dev/null 2>&1
MVDB_CASES=/mnt/e/mvdb/tests/hard_cases timeout 900 python3 -u tests/run_official_cases.py 2>/dev/null | tail -1
# independent re-implementation of the case comparison over the published package
printf '%-26s ' "official_cases (local)"
MVDB_CASES=/mnt/e/pw/round2/pkg2/mysqltest timeout 900 python3 -u tests/run_official_cases.py 2>/dev/null | tail -1
echo -n "official protocol_check PASS count: "
cd /mnt/e/pw/round2/pkg2 && ./run_local_check.sh 2>/dev/null | grep -cE ': PASS'
