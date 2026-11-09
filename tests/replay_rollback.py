#!/usr/bin/env python3
"""Replays the statement sequence the stateful fuzzer flagged, printing every response."""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from diff_fuzz import Session  # noqa: E402

SCRIPT = [
    ("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), content VARCHAR(16), embedding VECTOR(2));", None),
    ("CREATE VECTOR INDEX idx_emb ON docs (embedding);", None),
    ("INSERT INTO docs VALUES (8, '\u7532', '\u4e94', '[3, 0]');", None),
    ("DELETE FROM docs WHERE id = 8;", None),
    ("RESTART", None),
    ("INSERT INTO docs VALUES (1, '\u4e59', '\u4e00', '[2, 1]');", None),
    ("INSERT INTO docs VALUES (10, '\u7532', '\u4e8c', '[2, 1]');", None),
    ("INSERT INTO docs VALUES (5, '\u7532', '\u4e09', '[3, 0]');", None),
    ("INSERT INTO docs VALUES (11, '\u4e59', '\u56db', '[2, 1]');", None),
    ("INSERT INTO docs VALUES (2, '\u7532', '\u4e8c', '[3, 2]');", None),
    ("UPDATE docs SET content = '\u4e00' WHERE id = 2;", None),
    ("INSERT INTO docs VALUES (12, '\u4e19', '\u56db', '[1, 1]');", None),
    ("DELETE FROM docs WHERE id = 2;", None),
    ("DELETE FROM docs WHERE id = 11;", None),
    ("DELETE FROM docs WHERE id = 1;", None),
    ("UPDATE docs SET embedding = '[0, 1]' WHERE id = 12;", None),
    ("INSERT INTO docs VALUES (9, '\u4e59', '\u56db', '[0, 1]');", None),
    ("UPDATE docs SET embedding = '[0, 0]' WHERE id = 5;", None),
    ("INSERT INTO docs VALUES (4, '\u7532', '\u4e09', '[0, 2]');", None),
    ("BEGIN;", None),
    ("DELETE FROM docs WHERE id = 12;", None),
    ("INSERT INTO docs VALUES (12, '\u7532', '\u56db', '[1, 1]');", None),
    ("DELETE FROM docs WHERE id = 10;", None),
    ("UPDATE docs SET embedding = '[0, 1]' WHERE id = 4;", None),
    ("SELECT id, L2_DISTANCE(embedding, '[0.5, 0]') AS distance FROM docs ORDER BY distance LIMIT 5;", None),
    ("ROLLBACK;", None),
    ("SELECT * FROM docs;", None),
]


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_replay_")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir)
    session = Session(data_dir)
    try:
        for line in SCRIPT:
            sql = line[0]
            if sql == "RESTART":
                session.close()
                session = Session(data_dir)
                print("    ---- restarted")
                continue
            result = session.run(sql)
            kind = result["kind"]
            extra = ""
            if kind == "ok":
                extra = " affected=%d" % result["affected"]
            elif kind == "error":
                extra = " " + result["error"]
            elif kind == "rows":
                extra = " columns=%r rows=%r" % (result["columns"], result["rows"])
            print("%-8s %s%s" % (kind, sql, extra))
    finally:
        session.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
