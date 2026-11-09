#!/usr/bin/env bash
# Checks that a driver forwarding its directives on the SQL channel always gets a response, and that a
# shutdown command exits cleanly with the data intact.
set -u
cd /mnt/e/mvdb || exit 1
rm -rf /tmp/dir_probe && mkdir -p /tmp/dir_probe
./build/minivectordb --data-dir /tmp/dir_probe <<'SQL'
CREATE TABLE t (id BIGINT PRIMARY KEY);
INSERT INTO t VALUES (1);
--mode ann
INSERT INTO t VALUES (2);
--restart
INSERT INTO t VALUES (3);
--mode exact
SELECT id FROM t ORDER BY id;
quit
SQL
echo "exit code: $?"
echo "--- restart and read back:"
./build/minivectordb --data-dir /tmp/dir_probe <<'SQL'
SELECT id FROM t ORDER BY id;
SQL
rm -rf /tmp/dir_probe
