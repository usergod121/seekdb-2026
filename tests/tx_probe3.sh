#!/usr/bin/env bash
# Does a SELECT inside the transaction (plain, and the TopK shape) break ROLLBACK?
set -u
cd /mnt/e/mvdb || exit 1
run() {
  local label="$1"; local body="$2"
  rm -rf /tmp/tx_p3 && mkdir -p /tmp/tx_p3
  {
    echo "CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), content VARCHAR(16), embedding VECTOR(2));"
    echo "INSERT INTO docs VALUES (10, '甲', '二', '[2,1]');"
    echo "INSERT INTO docs VALUES (12, '丙', '四', '[0,1]');"
    echo "INSERT INTO docs VALUES (4, '甲', '三', '[0,2]');"
    printf '%s\n' "$body"
  } > /tmp/tx_p3.sql
  echo "===== $label"
  ./build/minivectordb --data-dir /tmp/tx_p3 < /tmp/tx_p3.sql | grep -v '^READY$' | grep -v '^BYE$'
}

run "plain select inside tx" 'BEGIN;
DELETE FROM docs WHERE id = 10;
SELECT id FROM docs;
ROLLBACK;
SELECT * FROM docs;'

run "topk select inside tx" 'BEGIN;
DELETE FROM docs WHERE id = 10;
SELECT id, L2_DISTANCE(embedding, '"'"'[0,0]'"'"') AS distance FROM docs ORDER BY distance LIMIT 5;
ROLLBACK;
SELECT * FROM docs;'

rm -rf /tmp/tx_p3 /tmp/tx_p3.sql
