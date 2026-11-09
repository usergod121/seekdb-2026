#!/usr/bin/env bash
# Rebuild and run the public mysqltest cases.
set -u
cd /mnt/e/mvdb || exit 1
make -C build -j8 2>&1 | grep -E "error:|Error|Built target" | head -20
echo "---- public cases ----"
python3 tests/run_public_cases.py
