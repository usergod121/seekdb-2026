#!/usr/bin/env bash
# Runs the suites against the ASan/UBSan build and reports the first sanitizer complaint, if any.
set -u
cd /mnt/e/mvdb || exit 1
export MVDB_BIN=/mnt/e/mvdb/build-asan/minivectordb
export ASAN_OPTIONS=detect_leaks=0:abort_on_error=1:print_stacktrace=1
export UBSAN_OPTIONS=print_stacktrace=1:halt_on_error=0
found=0
for t in boundary_suite boundary_suite2 probe_syntax3 probe_finite probe_schema probe_tx_matrix \
         probe_row_width probe_like_between probe_select_extras probe_mysql_syntax probe_bulk \
         probe_wide_rows probe_table_shapes probe_extreme_vectors probe_crash_shapes; do
  out=$(timeout 900 python3 -u "tests/$t.py" 2>&1)
  status=$?
  line=$(printf '%s\n' "$out" | grep -E "ERROR: AddressSanitizer|runtime error:|SUMMARY: UndefinedBehavior" | head -2)
  summary=$(printf '%s\n' "$out" | grep -E "problem|passed|no failures" | tail -1)
  printf '%-24s exit=%-3s %s\n' "$t" "$status" "${summary:-no summary}"
  if [ -n "$line" ]; then
    printf '  SANITIZER: %s\n' "$line"
    found=1
  fi
done
# the published package and the blind corpora as well
out=$(MVDB_BIN=/mnt/e/mvdb/build-asan/minivectordb MVDB_CASES=/mnt/e/pw/round2/pkg2/mysqltest \
      timeout 900 python3 -u tests/run_official_cases.py 2>&1)
printf '%-24s %s\n' "official cases" "$(printf '%s\n' "$out" | tail -1)"
printf '%s\n' "$out" | grep -E "ERROR: AddressSanitizer|runtime error:" | head -3
echo "sanitizer findings: $found"
