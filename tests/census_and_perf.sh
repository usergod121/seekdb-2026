#!/usr/bin/env bash
# Smoke test error census plus a perf spot check on the graded shape (10000 x 128, TopK 10).
set -u
cd /mnt/e/mvdb || exit 1
echo "==== smoke error census ===="
bash tests/run_smoke.sh > /tmp/smoke.out 2>&1
grep -c "^ERROR" /tmp/smoke.out || true
echo "smoke lines: $(wc -l < /tmp/smoke.out)"

echo "==== perf spot check ===="
rm -rf /tmp/mvdb_perf* 2>/dev/null
bash tests/perf_smoke.sh 2>&1 | tail -20
