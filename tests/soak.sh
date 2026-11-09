#!/usr/bin/env bash
# Long-running correctness soak: many seeds of both differential tests.
set -u
cd /mnt/e/mvdb || exit 1
echo "==== stateless fuzz, 20 seeds x 120 steps ===="
python3 tests/diff_fuzz.py --seeds 20 --steps 120 2>&1 | tail -40
echo "==== stateful fuzz, 10 seeds x 120 steps ===="
python3 tests/diff_fuzz_state.py --seeds 10 --steps 120 2>&1 | tail -40
