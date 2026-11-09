#!/usr/bin/env bash
# Minimal transaction probes: rollback of an update, of an insert, of a delete, and commit.
set -u
cd /mnt/e/mvdb || exit 1
rm -rf /tmp/tx_probe && mkdir -p /tmp/tx_probe
./build/minivectordb --data-dir /tmp/tx_probe <<'SQL'
CREATE TABLE t (id BIGINT PRIMARY KEY, v BIGINT);
INSERT INTO t VALUES (1, 1);
BEGIN;
UPDATE t SET v = 2 WHERE id = 1;
ROLLBACK;
SELECT id, v FROM t;
BEGIN;
INSERT INTO t VALUES (2, 2);
ROLLBACK;
SELECT id FROM t;
BEGIN;
DELETE FROM t WHERE id = 1;
ROLLBACK;
SELECT id FROM t;
BEGIN;
UPDATE t SET v = 9 WHERE id = 1;
COMMIT;
SELECT id, v FROM t;
SQL
rm -rf /tmp/tx_probe
