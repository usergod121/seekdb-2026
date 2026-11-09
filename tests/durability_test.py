#!/usr/bin/env python3
"""Durability across a clean restart, one shape at a time."""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from diff_fuzz import Session  # noqa: E402

SETUP = ("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), content VARCHAR(16), "
         "embedding VECTOR(2));")


def scenario(name, steps):
    tmp = tempfile.mkdtemp(prefix="mvdb_dur_")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir)
    session = Session(data_dir)
    problems = []
    try:
        session.run(SETUP)
        for sql in steps:
            result = session.run(sql)
            if result["kind"] == "error":
                problems.append("%s: %s -> %s" % (name, sql, result["error"]))
        before = session.run("SELECT * FROM docs;")["rows"]
        session.close()
        session = Session(data_dir)
        after = session.run("SELECT * FROM docs;")["rows"]
        if sorted(before) != sorted(after):
            problems.append("%s: after restart %r != %r" % (name, sorted(after), sorted(before)))
            same_count = len(before) == len(after)
            if not same_count:
                problems.append("%s:   row count %d != %d" % (name, len(after), len(before)))
    finally:
        session.close()
        shutil.rmtree(tmp, ignore_errors=True)
    print("%-42s %s" % (name, "ok" if not problems else "FAIL"))
    for problem in problems:
        print("    " + problem)
    return problems


def main():
    problems = []
    problems += scenario("plain inserts", [
        "INSERT INTO docs VALUES (1, '\u7532', '\u4e00', '[0,0]');",
        "INSERT INTO docs VALUES (2, '\u4e59', '\u4e8c', '[1,1]');",
        "INSERT INTO docs VALUES (3, '\u4e19', '\u4e09', '[2,2]');",
    ])
    problems += scenario("multi-row insert", [
        "INSERT INTO docs VALUES (1, '\u7532', '\u4e00', '[0,0]'), (2, '\u4e59', '\u4e8c', '[1,1]');",
    ])
    problems += scenario("insert then update", [
        "INSERT INTO docs VALUES (1, '\u7532', '\u4e00', '[0,0]');",
        "UPDATE docs SET embedding = '[3,4]' WHERE id = 1;",
    ])
    problems += scenario("insert then growing update", [
        "INSERT INTO docs VALUES (1, '\u7532', '\u4e00', '[0,0]');",
        "UPDATE docs SET content = '\u5df2\u66f4\u65b0' WHERE id = 1;",
    ])
    problems += scenario("insert then delete", [
        "INSERT INTO docs VALUES (1, '\u7532', '\u4e00', '[0,0]');",
        "INSERT INTO docs VALUES (2, '\u4e59', '\u4e8c', '[1,1]');",
        "DELETE FROM docs WHERE id = 2;",
    ])
    problems += scenario("committed transaction", [
        "BEGIN;",
        "INSERT INTO docs VALUES (1, '\u7532', '\u4e00', '[0,0]');",
        "COMMIT;",
    ])
    problems += scenario("rolled back transaction then insert", [
        "BEGIN;",
        "INSERT INTO docs VALUES (1, '\u7532', '\u4e00', '[0,0]');",
        "ROLLBACK;",
        "INSERT INTO docs VALUES (2, '\u4e59', '\u4e8c', '[1,1]');",
    ])
    problems += scenario("insert, select, insert", [
        "INSERT INTO docs VALUES (1, '\u7532', '\u4e00', '[0,0]');",
        "SELECT * FROM docs;",
        "INSERT INTO docs VALUES (2, '\u4e59', '\u4e8c', '[1,1]');",
    ])
    problems += scenario("topk select then insert", [
        "INSERT INTO docs VALUES (1, '\u7532', '\u4e00', '[0,0]');",
        "SELECT id, L2_DISTANCE(embedding, '[0,0]') AS distance FROM docs ORDER BY distance LIMIT 5;",
        "INSERT INTO docs VALUES (2, '\u4e59', '\u4e8c', '[1,1]');",
    ])
    problems += scenario("vector index then inserts", [
        "CREATE VECTOR INDEX idx ON docs (embedding);",
        "INSERT INTO docs VALUES (1, '\u7532', '\u4e00', '[0,0]');",
    ])
    problems += scenario("index, many inserts", [
        "CREATE VECTOR INDEX idx ON docs (embedding);",
    ] + ["INSERT INTO docs VALUES (%d, '\u7532', '\u4e00', '[%d,0]');" % (i, i)
         for i in range(1, 9)])
    print()
    print("%d problem(s)" % len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
