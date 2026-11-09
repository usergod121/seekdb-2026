#!/usr/bin/env bash
# Bisects which statement inside a transaction breaks ROLLBACK.
set -u
cd /mnt/e/mvdb || exit 1
BASE="CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), content VARCHAR(16), embedding VECTOR(2));
INSERT INTO docs VALUES (10, '甲', '二', '[2,1]');
INSERT INTO docs VALUES (12, '丙', '四', '[0,1]');
INSERT INTO docs VALUES (4, '甲', '三', '[0,2]');"

run() {
  local label="$1"; shift
  local body="$1"
  rm -rf /tmp/tx_bisect && mkdir -p /tmp/tx_bisect
  printf '%s\n%s\n' "$BASE" "$body" > /tmp/tx_bisect.sql
  echo "===== $label"
  ./build/minivectordb --data-dir /tmp/tx_bisect < /tmp/tx_bisect.sql | tail -8
}

run "one update" 'BEGIN;
UPDATE docs SET embedding = '"'"'[0,1]'"'"' WHERE id = 4;
ROLLBACK;
SELECT id, embedding FROM docs;'

run "one update + select inside tx" 'BEGIN;
UPDATE docs SET embedding = '"'"'[0,1]'"'"' WHERE id = 4;
SELECT id FROM docs;
ROLLBACK;
SELECT id, embedding FROM docs;'

run "delete+insert same pk" 'BEGIN;
DELETE FROM docs WHERE id = 12;
INSERT INTO docs VALUES (12, '"'"'甲'"'"', '"'"'四'"'"', '"'"'[1,1]'"'"');
ROLLBACK;
SELECT id, content FROM docs;'

run "four statements" 'BEGIN;
DELETE FROM docs WHERE id = 12;
INSERT INTO docs VALUES (12, '"'"'甲'"'"', '"'"'四'"'"', '"'"'[1,1]'"'"');
DELETE FROM docs WHERE id = 10;
UPDATE docs SET embedding = '"'"'[0,1]'"'"' WHERE id = 4;
ROLLBACK;
SELECT id, content, embedding FROM docs;'

rm -rf /tmp/tx_bisect /tmp/tx_bisect.sql
