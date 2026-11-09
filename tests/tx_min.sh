#!/usr/bin/env bash
# Minimal reproduction: the inserted row survives ROLLBACK when a SELECT runs in between.
set -u
cd /mnt/e/mvdb || exit 1
rm -rf /tmp/tx_min && mkdir -p /tmp/tx_min
MVDB_DEBUG_UNDO=1 ./build/minivectordb --data-dir /tmp/tx_min 2>&1 <<'SQL'
CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), content VARCHAR(16), embedding VECTOR(2));
BEGIN;
INSERT INTO docs VALUES (12, '甲', '四', '[1, 1]');
SELECT id, L2_DISTANCE(embedding, '[0.5, 0]') AS distance FROM docs ORDER BY distance LIMIT 5;
ROLLBACK;
SELECT * FROM docs;
SQL
rm -rf /tmp/tx_min
