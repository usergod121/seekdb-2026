#!/usr/bin/env bash
# Correctness gate for the parallel scan: public cases, both fuzzers, smoke, crash recovery, and the
# scale check with recall verification.
set -u
cd /mnt/e/mvdb || exit 1
echo "==== public cases ===="
python3 tests/run_public_cases.py | tail -3
echo "==== differential fuzz (stateless) ===="
python3 tests/diff_fuzz.py --seeds 6 --steps 80 | tail -2
echo "==== differential fuzz (stateful) ===="
python3 tests/diff_fuzz_state.py --seeds 4 --steps 80 | tail -2
echo "==== durability ===="
python3 tests/durability_test.py | tail -2
echo "==== smoke ===="
bash tests/run_smoke.sh > /tmp/smoke.out 2>&1; echo "errors: $(grep -c '^ERROR' /tmp/smoke.out || true)"
echo "==== crash recovery ===="
bash tests/crash_test.sh 2>&1 | tail -3
echo "==== scale + recall ===="
python3 tests/scale_check.py --rows 10000 --queries 400 --check 24 2>&1 | tail -5
