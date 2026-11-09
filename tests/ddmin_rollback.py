#!/usr/bin/env python3
"""Delta-debugs the failing statement sequence down to a minimal reproduction.

The invariant is state-independent: the whole table right after BEGIN must equal the whole table right
after ROLLBACK. A candidate sequence is interesting when that invariant breaks, which lets the minimizer
drop statements without knowing what the workload was supposed to be.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from diff_fuzz import Session  # noqa: E402

HISTORY = [
    "CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), content VARCHAR(16), embedding VECTOR(2));",
    "CREATE VECTOR INDEX idx_emb ON docs (embedding);",
    "INSERT INTO docs VALUES (8, '\u7532', '\u4e94', '[3, 0]');",
    "DELETE FROM docs WHERE id = 8;",
    "INSERT INTO docs VALUES (1, '\u4e59', '\u4e00', '[2, 1]');",
    "INSERT INTO docs VALUES (10, '\u7532', '\u4e8c', '[2, 1]');",
    "INSERT INTO docs VALUES (5, '\u7532', '\u4e09', '[3, 0]');",
    "INSERT INTO docs VALUES (11, '\u4e59', '\u56db', '[2, 1]');",
    "INSERT INTO docs VALUES (2, '\u7532', '\u4e8c', '[3, 2]');",
    "UPDATE docs SET content = '\u4e00' WHERE id = 2;",
    "INSERT INTO docs VALUES (12, '\u4e19', '\u56db', '[1, 1]');",
    "DELETE FROM docs WHERE id = 2;",
    "DELETE FROM docs WHERE id = 11;",
    "DELETE FROM docs WHERE id = 1;",
    "UPDATE docs SET embedding = '[0, 1]' WHERE id = 12;",
    "INSERT INTO docs VALUES (9, '\u4e59', '\u56db', '[0, 1]');",
    "UPDATE docs SET embedding = '[0, 0]' WHERE id = 5;",
    "INSERT INTO docs VALUES (4, '\u7532', '\u4e09', '[0, 2]');",
]
TX = [
    "DELETE FROM docs WHERE id = 12;",
    "INSERT INTO docs VALUES (12, '\u7532', '\u56db', '[1, 1]');",
    "DELETE FROM docs WHERE id = 10;",
    "UPDATE docs SET embedding = '[0, 1]' WHERE id = 4;",
    "SELECT id, L2_DISTANCE(embedding, '[0.5, 0]') AS distance FROM docs ORDER BY distance LIMIT 5;",
]


def snapshot(session):
    result = session.run("SELECT * FROM docs;")
    if result["kind"] != "rows":
        return None
    return sorted(tuple(row) for row in result["rows"])


def fails(history, tx):
    """True when the statement list reproduces the broken invariant or an unexpected error."""
    tmp = tempfile.mkdtemp(prefix="mvdb_dd_")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir)
    session = Session(data_dir)
    try:
        for sql in history:
            if session.run(sql)["kind"] == "error":
                return False
        before = snapshot(session)
        if before is None:
            return False
        session.run("BEGIN;")
        for sql in tx:
            if session.run(sql)["kind"] == "error":
                return False
        session.run("ROLLBACK;")
        after = snapshot(session)
        return after is not None and after != before
    finally:
        session.close()
        shutil.rmtree(tmp, ignore_errors=True)


def minimize(history, tx):
    if not fails(history, tx):
        print("the full sequence does not reproduce")
        return history, tx
    changed = True
    while changed:
        changed = False
        for index in range(len(history) - 1, -1, -1):
            candidate = history[:index] + history[index + 1:]
            if fails(candidate, tx):
                history = candidate
                changed = True
                break
        if changed:
            continue
        for index in range(len(tx) - 1, -1, -1):
            candidate = tx[:index] + tx[index + 1:]
            if fails(history, candidate):
                tx = candidate
                changed = True
                break
    return history, tx


def main():
    history, tx = minimize(list(HISTORY), list(TX))
    print("minimal sequence that breaks ROLLBACK:")
    for sql in history:
        print("  (setup) %s" % sql)
    print("  BEGIN;")
    for sql in tx:
        print("  %s" % sql)
    print("  ROLLBACK;")


if __name__ == "__main__":
    main()
