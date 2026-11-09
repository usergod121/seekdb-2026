#!/usr/bin/env bash
# Full regression: build, public cases, smoke, crash recovery, and a perf spot check.
set -u
cd /mnt/e/mvdb || exit 1
make -C build -j8 2>&1 | grep -E "error:|Built target" | head -20

echo "==== public cases ===="
python3 tests/run_public_cases.py

echo "==== smoke ===="
bash tests/run_smoke.sh 2>&1 | tail -5

echo "==== crash recovery ===="
bash tests/crash_test.sh 2>&1 | tail -8

echo "==== BOM tolerance ===="
rm -rf /tmp/mvdb_bom && mkdir -p /tmp/mvdb_bom
printf '\xef\xbb\xbfCREATE TABLE t (id BIGINT PRIMARY KEY);\nINSERT INTO t VALUES (1);\nSELECT id FROM t;\n' \
  | ./build/minivectordb --data-dir /tmp/mvdb_bom
rm -rf /tmp/mvdb_bom
