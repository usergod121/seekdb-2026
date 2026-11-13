#!/usr/bin/env bash
# Re-runs the matrices that cover the cached scan paths after an engine change.
set -u
cd /mnt/e/mvdb || exit 1
for t in probe_matrix probe_order_matrix probe_matrix_writes probe_scale_model probe_crash_shapes probe_select_extras probe_mysql_syntax probe_like_between probe_reject_all probe_extreme_vectors probe_predicate_types probe_protocol_fuzz; do
  printf '%-24s ' "$t"
  python3 -u "tests/$t.py" 2>&1 | tail -1
done
