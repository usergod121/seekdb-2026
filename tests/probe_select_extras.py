#!/usr/bin/env python3
"""Select-list extras that a case is likely to use: LIMIT with an offset and IN lists.

MySQL spells a row offset as "LIMIT offset, count" or "LIMIT count OFFSET offset", and filters a list
with "col IN (v1, v2, ...)". Neither appears in the public samples, so both are checked here against
the exact expected rows, including through the vector TopK path and inside a conjunctive filter.
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
    tmp = tempfile.mkdtemp(prefix="mvdb_sel_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        s = S(data)
        s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, note VARCHAR(8), emb VECTOR(2))")
        s.req("INSERT INTO t VALUES (1,'a','[1,0]'),(2,'b','[0,1]'),(3,'c','[1,1]'),(4,'a','[2,2]'),"
              "(5,'b','[3,3]')")
        # distances to [1,0]: 1 -> 0, 2 -> 1.414, 3 -> 1, 4 -> 2.236, 5 -> 2.828
        cases = [
            ("LIMIT 2,3", "SELECT id FROM t ORDER BY id LIMIT 2,3", [3, 4, 5]),
            ("LIMIT 2 OFFSET 1", "SELECT id FROM t ORDER BY id LIMIT 2 OFFSET 1", [2, 3]),
            ("LIMIT 3 OFFSET 0", "SELECT id FROM t ORDER BY id LIMIT 3 OFFSET 0", [1, 2, 3]),
            ("offset past the end", "SELECT id FROM t ORDER BY id LIMIT 3 OFFSET 9", []),
            ("limit zero with offset", "SELECT id FROM t ORDER BY id LIMIT 0 OFFSET 2", []),
            ("plain LIMIT unchanged", "SELECT id FROM t ORDER BY id LIMIT 3", [1, 2, 3]),
            ("order by DESC with offset", "SELECT id FROM t ORDER BY id DESC LIMIT 2 OFFSET 1", [4, 3]),
            ("topk with offset", "SELECT id FROM t ORDER BY L2_DISTANCE(emb,'[1,0]') LIMIT 2 OFFSET 1",
             [3, 2]),
            ("topk offset-comma form",
             "SELECT id FROM t ORDER BY L2_DISTANCE(emb,'[1,0]') LIMIT 1,2", [3, 2]),
            ("filtered topk with offset",
             "SELECT id FROM t WHERE note='a' ORDER BY L2_DISTANCE(emb,'[1,0]') LIMIT 1 OFFSET 1",
             [4]),
            ("IN on BIGINT", "SELECT id FROM t WHERE id IN (2,4) ORDER BY id", [2, 4]),
            ("IN on VARCHAR", "SELECT id FROM t WHERE note IN ('a','c') ORDER BY id", [1, 3, 4]),
            ("IN with one value", "SELECT id FROM t WHERE note IN ('b') ORDER BY id", [2, 5]),
            ("IN inside AND", "SELECT id FROM t WHERE note IN ('a','b') AND id>1 ORDER BY id",
             [2, 4, 5]),
            ("IN with no match", "SELECT id FROM t WHERE id IN (99,100) ORDER BY id", []),
            ("IN combined with LIMIT",
             "SELECT id FROM t WHERE note IN ('a','b') ORDER BY id LIMIT 2", [1, 2]),
            ("IN with an offset",
             "SELECT id FROM t WHERE note IN ('a','b') ORDER BY id LIMIT 1 OFFSET 1", [2]),
        ]
        for label, sql, expected in cases:
            reply = s.req(sql)
            check(label, ids(reply) == expected, "want %r got %r" % (expected, ids(reply)))

        # shapes that stay unsupported must still fail loudly rather than being reinterpreted
        for label, sql in [
            ("IN with an empty list", "SELECT id FROM t WHERE id IN ()"),
            ("negative offset", "SELECT id FROM t LIMIT 2 OFFSET -1"),
            ("negative limit", "SELECT id FROM t LIMIT -2"),

            ("BETWEEN with a bad bound", "SELECT id FROM t WHERE id BETWEEN 1"),
            ("DISTINCT", "SELECT DISTINCT note FROM t"),
            ("GROUP BY", "SELECT note FROM t GROUP BY note"),
        ]:
            check(label + " is rejected", s.req(sql).get("ok") is False,
                  json.dumps(s.req(sql))[:140])

        check("table intact after the rejections", ids(s.req("SELECT id FROM t ORDER BY id"))
              == [1, 2, 3, 4, 5])
        s.close()
        s = S(data)
        check("offset query after restart", ids(s.req("SELECT id FROM t ORDER BY id LIMIT 2,2"))
              == [3, 4])
        check("IN query after restart",
              ids(s.req("SELECT id FROM t WHERE note IN ('c')")) == [3])
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
