#!/usr/bin/env bash
# Reproduces the rollback case the stateful fuzzer flagged: delete and re-insert the same key inside one
# transaction, plus a vector update, then roll back.
set -u
cd /mnt/e/mvdb || exit 1
rm -rf /tmp/tx_probe2 && mkdir -p /tmp/tx_probe2
./build/minivectordb --data-dir /tmp/tx_probe2 <<'SQL'
CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), content VARCHAR(16), embedding VECTOR(2));
INSERT INTO docs VALUES (10, '甲', '二', '[2,1]');
INSERT INTO docs VALUES (12, '丙', '四', '[0,1]');
INSERT INTO docs VALUES (4, '甲', '三', '[0,2]');
SELECT * FROM docs;
BEGIN;
DELETE FROM docs WHERE id = 12;
INSERT INTO docs VALUES (12, '甲', '四', '[1,1]');
DELETE FROM docs WHERE id = 10;
UPDATE docs SET embedding = '[0,1]' WHERE id = 4;
SELECT * FROM docs;
ROLLBACK;
SELECT * FROM docs;
SQL
rm -rf /tmp/tx_probe2
