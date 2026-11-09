#!/usr/bin/env bash
# Full correctness sweep after the transaction fixes.
set -u
cd /mnt/e/mvdb || exit 1
echo "==== public cases ===="
python3 tests/run_public_cases.py | tail -3
echo "==== differential fuzz (stateless) ===="
python3 tests/diff_fuzz.py --seeds 6 --steps 80 | tail -3
echo "==== differential fuzz (transactions, restart, index) ===="
python3 tests/diff_fuzz_state.py --seeds 4 --steps 70 | tail -6
echo "==== smoke ===="
bash tests/run_smoke.sh > /tmp/smoke.out 2>&1; echo "errors: $(grep -c '^ERROR' /tmp/smoke.out || true)"
echo "==== crash recovery ===="
bash tests/crash_test.sh 2>&1 | tail -4
echo "==== explicit vector projection ===="
rm -rf /tmp/mvdb_vp && mkdir -p /tmp/mvdb_vp
./build/minivectordb --data-dir /tmp/mvdb_vp <<'SQL'
CREATE TABLE docs (id BIGINT PRIMARY KEY, embedding VECTOR(2));
INSERT INTO docs VALUES (1, '[1.5, -2]');
SELECT id, embedding FROM docs;
SELECT embedding FROM docs;
SQL
rm -rf /tmp/mvdb_vp
