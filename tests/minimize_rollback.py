#!/usr/bin/env python3
"""Minimizes the ROLLBACK failure down to the fewest statements."""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from diff_fuzz import Session  # noqa: E402

SETUP = [
    "CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), content VARCHAR(16), embedding VECTOR(2));",
    "INSERT INTO docs VALUES (10, '\u7532', '\u4e8c', '[2,1]');",
    "INSERT INTO docs VALUES (12, '\u4e19', '\u56db', '[0,1]');",
    "INSERT INTO docs VALUES (4, '\u7532', '\u4e09', '[0,2]');",
]
BEFORE = {"10": ("\u7532", "\u4e8c", "[2,1]"),
          "12": ("\u4e19", "\u56db", "[0,1]"),
          "4": ("\u7532", "\u4e09", "[0,2]")}

VARIANTS = {
    "A delete+insert same pk, rollback":
        ["BEGIN;", "DELETE FROM docs WHERE id = 12;",
         "INSERT INTO docs VALUES (12, '\u7532', '\u56db', '[1,1]');", "ROLLBACK;"],
    "B delete only, rollback":
        ["BEGIN;", "DELETE FROM docs WHERE id = 12;", "ROLLBACK;"],
    "C insert only, rollback":
        ["BEGIN;", "INSERT INTO docs VALUES (13, '\u4e59', '\u4e00', '[1,1]');", "ROLLBACK;"],
    "D update vector, rollback":
        ["BEGIN;", "UPDATE docs SET embedding = '[0,1]' WHERE id = 4;", "ROLLBACK;"],
    "E update content, rollback":
        ["BEGIN;", "UPDATE docs SET content = '\u4e00' WHERE id = 4;", "ROLLBACK;"],
    "F delete+insert, then another delete, rollback":
        ["BEGIN;", "DELETE FROM docs WHERE id = 12;",
         "INSERT INTO docs VALUES (12, '\u7532', '\u56db', '[1,1]');",
         "DELETE FROM docs WHERE id = 10;", "ROLLBACK;"],
    "G delete+insert, then update, rollback":
        ["BEGIN;", "DELETE FROM docs WHERE id = 12;",
         "INSERT INTO docs VALUES (12, '\u7532', '\u56db', '[1,1]');",
         "UPDATE docs SET embedding = '[0,1]' WHERE id = 4;", "ROLLBACK;"],
    "H two deletes, rollback":
        ["BEGIN;", "DELETE FROM docs WHERE id = 12;", "DELETE FROM docs WHERE id = 10;", "ROLLBACK;"],
}


def state_rows(session):
    result = session.run("SELECT * FROM docs;")
    rows = {}
    for row in result["rows"]:
        rows[row[0]] = (row[1], row[2], row[3])
    return rows


def main():
    for label, statements in VARIANTS.items():
        tmp = tempfile.mkdtemp(prefix="mvdb_min_")
        data_dir = os.path.join(tmp, "data")
        os.makedirs(data_dir)
        session = Session(data_dir)
        try:
            for sql in SETUP:
                session.run(sql)
            for sql in statements:
                result = session.run(sql)
                if result["kind"] == "error":
                    print("%s: statement failed: %s -> %s" % (label, sql, result["error"]))
            rows = state_rows(session)
        finally:
            session.close()
            shutil.rmtree(tmp, ignore_errors=True)
        same = rows == BEFORE
        print("%-46s %s" % (label, "rollback ok" if same else "ROLLBACK LOST: %r" % (rows,)))


if __name__ == "__main__":
    main()
