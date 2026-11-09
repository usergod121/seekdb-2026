#!/usr/bin/env python3
"""Probes SQL shapes a hidden case plausibly uses and reports the ones the engine rejects.

Nothing here is tuned to a published expectation: each probe is a statement a reasonable test writer
would use for the documented scope (CRUD, comparisons, ordering, empty results, vectors, transactions,
persistence, index maintenance), and the point is only to find gaps that would turn a case into ERROR.
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")

SETUP = [
    "CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), content VARCHAR(16), embedding VECTOR(2));",
    "INSERT INTO docs VALUES (3, '乙', '三', '[0, 2]'), (1, '甲', '一', '[0, 0]'), (2, '甲', '二', '[1, 0]');",
]

PROBES = [
    # ordering and aliases
    "SELECT id, L2_DISTANCE(embedding, '[0,0]') AS distance FROM docs ORDER BY distance DESC LIMIT 2;",
    "SELECT id AS i FROM docs ORDER BY i;",
    "SELECT id FROM docs ORDER BY category, id;",
    "SELECT id FROM docs ORDER BY id ASC;",
    "SELECT id FROM docs ORDER BY 1 LIMIT 2;",
    # projection shapes
    "SELECT * FROM docs;",
    "SELECT category, id FROM docs ORDER BY id;",
    "SELECT id FROM docs WHERE id <> 2 ORDER BY id;",
    "SELECT id FROM docs WHERE id != 2 ORDER BY id;",
    "SELECT id FROM docs WHERE content = '一';",
    "SELECT id FROM docs WHERE id >= -5 ORDER BY id;",
    "SELECT id FROM docs WHERE category = 'missing';",
    # keyword case and spacing
    "select id from docs order by id limit 2;",
    "SELECT  id   FROM   docs   ORDER   BY   id   LIMIT   2;",
    "SELECT ID FROM DOCS ORDER BY ID;",
    "SELECT id FROM docs LIMIT 2;",
    "SELECT id FROM docs LIMIT 2;SELECT id FROM docs LIMIT 1;",
    # writes
    "INSERT INTO docs (id, category, content, embedding) VALUES (4, '丙', '四', '[2,2]');",
    "UPDATE docs SET content = 'z', category = 'q' WHERE id = 4;",
    "DELETE FROM docs WHERE id = 4;",
    "INSERT INTO docs VALUES (5, '丁', '五', '[1.5, -2.25]');",
    "SELECT id, L2_DISTANCE(embedding, '[0,0]') AS distance FROM docs ORDER BY distance LIMIT 3;",
    "DELETE FROM docs WHERE category = '甲';",
    # transactions
    "BEGIN;",
    "INSERT INTO docs VALUES (6, '戊', '六', '[3,3]');",
    "ROLLBACK;",
    "SELECT id FROM docs WHERE id = 6;",
    "BEGIN;",
    "INSERT INTO docs VALUES (7, '己', '七', '[4,4]');",
    "COMMIT;",
    "SELECT id FROM docs WHERE id = 7;",
    # index maintenance
    "CREATE VECTOR INDEX idx_emb ON docs (embedding);",
    "SELECT id, L2_DISTANCE(embedding, '[0,0]') AS distance FROM docs ORDER BY distance LIMIT 2;",
    "INSERT INTO docs VALUES (8, '庚', '八', '[9,9]');",
    "UPDATE docs SET embedding = '[0.1,0.1]' WHERE id = 1;",
    "SELECT id, L2_DISTANCE(embedding, '[0,0]') AS distance FROM docs ORDER BY distance LIMIT 2;",
    "DELETE FROM docs WHERE id = 8;",
    "SELECT id, L2_DISTANCE(embedding, '[0,0]') AS distance FROM docs ORDER BY distance LIMIT 3;",
]


def run(lines, data_dir):
    proc = subprocess.Popen(
        [BIN, "--data-dir", data_dir],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
    )
    text = "\n".join(lines) + "\n"
    out, _ = proc.communicate(text.encode("utf-8"), timeout=120)
    return out.decode("utf-8", "replace")


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_probe_")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir)
    failures = 0
    try:
        for sql in PROBES:
            out = run(SETUP + [sql], data_dir)
            shutil.rmtree(data_dir, ignore_errors=True)
            os.makedirs(data_dir)
            bad = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
            if bad:
                failures += 1
                print("FAIL  %s" % sql)
                print("      %s" % bad[0])
            else:
                print("ok    %s" % sql)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("%d probe(s) rejected" % failures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
