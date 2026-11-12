#!/usr/bin/env bash
# Shows the failing checks of the suites that regressed.
set -u
cd /mnt/e/mvdb || exit 1
for t in probe_finite probe_schema probe_like_between boundary_suite probe_syntax2; do
  echo "=== $t"
  python3 -u "tests/$t.py" 2>&1 | grep -E 'FAIL|problem' | head -6
done
