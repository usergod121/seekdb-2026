#!/usr/bin/env python3
"""MySQL spellings a case is likely to use: table aliases, qualified columns, DROP/TRUNCATE and the
negated predicates.

All of these were syntax errors before, which is the one class of failure that does not show up in the
public samples: they only use the plainest spelling. A test case written by hand reaches for
`SELECT d.id FROM docs AS d`, `DROP TABLE IF EXISTS`, `TRUNCATE TABLE`, `NOT IN`, `NOT LIKE`,
`IS NOT NULL` and qualified names almost by habit.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
PROBLEMS = []
CHECKS = [0]


class S:
    def __init__(self, data):
        self.proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""
        self.seq = 0
        self.line()

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 16)
            if not chunk:
                raise RuntimeError("engine closed its output")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql):
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": "exact"}) + "\n").encode())
        return json.loads(self.line())

    def close(self):
        try:
            self.seq += 1
            os.write(self.proc.stdin.fileno(),
                     (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
            self.line()
            self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()


def check(label, condition, detail=""):
    CHECKS[0] += 1
    if not condition:
        PROBLEMS.append(label + ("  " + detail if detail else ""))
        print("FAIL  " + label + ("  " + detail if detail else ""))


def ids(reply):
    if not reply.get("ok"):
        return None
    cols = reply.get("columns") or []
    if "id" not in cols:
        return None
    i = cols.index("id")
    return [row[i] for row in (reply.get("rows") or [])]


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_mysql_syntax_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        s = S(data)
        s.req("CREATE TABLE docs (id BIGINT PRIMARY KEY, note VARCHAR(8), emb VECTOR(2))")
        s.req("INSERT INTO docs VALUES (1,'a','[1,0]'),(2,'b','[0,1]'),(3,'ab','[1,1]'),"
              "(4,'','[2,2]')")

        # qualified column names, with and without an alias
        check("qualified column without alias",
              ids(s.req("SELECT docs.id FROM docs ORDER BY docs.id")) == [1, 2, 3, 4])
        check("table alias with AS",
              ids(s.req("SELECT d.id FROM docs AS d ORDER BY d.id")) == [1, 2, 3, 4])
        check("table alias without AS",
              ids(s.req("SELECT d.id FROM docs d ORDER BY d.id")) == [1, 2, 3, 4])
        check("alias in WHERE and ORDER BY",
              ids(s.req("SELECT d.id FROM docs AS d WHERE d.id > 1 ORDER BY d.id")) == [2, 3, 4])
        check("alias with a distance projection",
              ids(s.req("SELECT t.id FROM docs AS t ORDER BY L2_DISTANCE(t.emb,'[1,0]') LIMIT 1"))
              == [1])

        # negated predicates
        check("NOT IN", ids(s.req("SELECT id FROM docs WHERE id NOT IN (1,2) ORDER BY id")) == [3, 4])
        check("NOT IN with one value",
              ids(s.req("SELECT id FROM docs WHERE id NOT IN (4) ORDER BY id")) == [1, 2, 3])
        check("NOT LIKE", ids(s.req("SELECT id FROM docs WHERE note NOT LIKE 'a%' ORDER BY id"))
              == [2, 4])
        check("NOT BETWEEN",
              ids(s.req("SELECT id FROM docs WHERE id NOT BETWEEN 2 AND 3 ORDER BY id")) == [1, 4])
        check("NOT IN inside AND",
              ids(s.req("SELECT id FROM docs WHERE id NOT IN (1) AND note LIKE 'a%' ORDER BY id"))
              == [3])
        check("IS NULL is false for every stored row",
              ids(s.req("SELECT id FROM docs WHERE note IS NULL")) == [])
        check("IS NOT NULL matches every row",
              ids(s.req("SELECT id FROM docs WHERE note IS NOT NULL ORDER BY id")) == [1, 2, 3, 4])
        check("IS NOT NULL inside AND",
              ids(s.req("SELECT id FROM docs WHERE note IS NOT NULL AND id < 3 ORDER BY id"))
              == [1, 2])

        # TRUNCATE
        reply = s.req("TRUNCATE TABLE docs")
        check("TRUNCATE accepted", reply.get("ok") is True, json.dumps(reply)[:140])
        check("TRUNCATE emptied the table", ids(s.req("SELECT id FROM docs")) == [])
        check("insert works after TRUNCATE",
              s.req("INSERT INTO docs VALUES (7,'z','[0,0]')").get("ok") is True)
        check("row after TRUNCATE", ids(s.req("SELECT id FROM docs")) == [7])

        # DROP TABLE, with and without IF EXISTS
        check("DROP TABLE accepted", s.req("DROP TABLE docs").get("ok") is True)
        check("table gone after DROP", s.req("SELECT id FROM docs").get("ok") is False)
        check("DROP TABLE IF EXISTS on a missing table is a no-op",
              s.req("DROP TABLE IF EXISTS docs").get("ok") is True)
        check("DROP TABLE on a missing table fails",
              s.req("DROP TABLE docs").get("ok") is False)
        check("recreate after DROP",
              s.req("CREATE TABLE docs (id BIGINT PRIMARY KEY, emb VECTOR(2))").get("ok") is True)
        check("insert into the recreated table",
              s.req("INSERT INTO docs VALUES (1,'[0.5,0.5]')").get("ok") is True)
        check("query the recreated table", ids(s.req("SELECT id FROM docs")) == [1])

        # and the same after a restart: the catalog must agree with the files
        s.close()
        s = S(data)
        check("recreated table survives a restart", ids(s.req("SELECT id FROM docs")) == [1])
        check("dropped table stays dropped", ids(s.req("SELECT id FROM gone")) is None)
        check("DROP after restart", s.req("DROP TABLE docs").get("ok") is True)
        s.close()
        s = S(data)
        check("drop survives a restart", s.req("SELECT id FROM docs").get("ok") is False)
        s.close()
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
    print()
    print("checks: %d" % CHECKS[0])
    if PROBLEMS:
        print("problems: %d" % len(PROBLEMS))
        for p in PROBLEMS:
            print("  - " + p)
    else:
        print("no problems")
    sys.exit(1 if PROBLEMS else 0)
