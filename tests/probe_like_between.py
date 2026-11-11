#!/usr/bin/env python3
"""LIKE and BETWEEN, the two remaining pieces of ordinary MySQL filter spelling.

Neither is required by the task statement, but a case that filters text naturally reaches for them,
and the engine answered them with a syntax error. The expectations here are the documented MySQL
semantics: '%' matches any run (including nothing), '_' exactly one character, BETWEEN is inclusive on
both ends, and both compose with AND, LIMIT and the vector TopK.
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


def ids(reply):
    if not reply.get("ok"):
        return None
    cols = reply.get("columns") or []
    if "id" not in cols:
        return None
    i = cols.index("id")
    return [row[i] for row in (reply.get("rows") or [])]


def check(label, condition, detail=""):
    CHECKS[0] += 1
    if not condition:
        PROBLEMS.append(label + ("  " + detail if detail else ""))
        print("FAIL  " + label + ("  " + detail if detail else ""))


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_like_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        s = S(data)
        s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, note VARCHAR(8), emb VECTOR(2))")
        s.req("INSERT INTO t VALUES (1,'abc','[1,0]'),(2,'abd','[0,1]'),(3,'xzb','[1,1]'),"
              "(4,'q','[2,2]')")

        cases = [
            ("LIKE prefix", "SELECT id FROM t WHERE note LIKE 'ab%' ORDER BY id", [1, 2]),
            ("LIKE suffix", "SELECT id FROM t WHERE note LIKE '%b' ORDER BY id", [3]),
            ("LIKE contains", "SELECT id FROM t WHERE note LIKE '%b%' ORDER BY id", [1, 2, 3]),
            ("LIKE one character", "SELECT id FROM t WHERE note LIKE 'a_c' ORDER BY id", [1]),
            ("LIKE matches everything", "SELECT id FROM t WHERE note LIKE '%%' ORDER BY id",
             [1, 2, 3, 4]),
            ("LIKE matches nothing", "SELECT id FROM t WHERE note LIKE 'zz%' ORDER BY id", []),
            ("LIKE on an empty string", "SELECT id FROM t WHERE note LIKE '%' AND id=4 ORDER BY id",
             [4]),
            ("LIKE on a BIGINT column", "SELECT id FROM t WHERE id LIKE '1%' ORDER BY id", [1]),
            ("LIKE with AND", "SELECT id FROM t WHERE note LIKE 'ab%' AND id>1 ORDER BY id", [2]),
            ("BETWEEN numeric", "SELECT id FROM t WHERE id BETWEEN 2 AND 3 ORDER BY id", [2, 3]),
            ("BETWEEN is inclusive", "SELECT id FROM t WHERE id BETWEEN 1 AND 1 ORDER BY id", [1]),
            ("BETWEEN outside", "SELECT id FROM t WHERE id BETWEEN 9 AND 12 ORDER BY id", []),
            ("BETWEEN text", "SELECT id FROM t WHERE note BETWEEN 'ab' AND 'abc' ORDER BY id", [1]),
            ("BETWEEN with AND", "SELECT id FROM t WHERE id BETWEEN 1 AND 4 AND note LIKE 'a%' "
                                 "ORDER BY id", [1, 2]),
            ("LIKE with an offset",
             "SELECT id FROM t WHERE note LIKE 'a%' ORDER BY id LIMIT 1 OFFSET 1", [2]),
            ("LIKE inside a TopK filter",
             "SELECT id FROM t WHERE note LIKE 'a%' ORDER BY L2_DISTANCE(emb,'[1,0]') LIMIT 1", [1]),
            # distances to [1,0]: id 3 ([1,1]) is 1.0, id 2 ([0,1]) is 1.414
            ("BETWEEN inside a TopK filter",
             "SELECT id FROM t WHERE id BETWEEN 2 AND 3 ORDER BY L2_DISTANCE(emb,'[1,0]') LIMIT 4",
             [3, 2]),
            ("LIKE on a vector column matches nothing",
             "SELECT id FROM t WHERE emb LIKE '%1%' ORDER BY id", []),
        ]
        for label, sql, expected in cases:
            reply = s.req(sql)
            check(label, ids(reply) == expected, "want %r got %r" % (expected, ids(reply)))

        # the statements that remain unsupported must still fail loudly
        for label, sql in [("DISTINCT", "SELECT DISTINCT note FROM t"),
                           ("GROUP BY", "SELECT note FROM t GROUP BY note"),
                           ("arith", "SELECT id + 1 FROM t"),
                           ("IS NULL", "SELECT id FROM t WHERE note IS NULL")]:
            check(label + " is rejected", s.req(sql).get("ok") is False, json.dumps(s.req(sql))[:120])

        s.close()
        s = S(data)
        check("LIKE after restart",
              ids(s.req("SELECT id FROM t WHERE note LIKE '%b%' ORDER BY id")) == [1, 2, 3])
        check("BETWEEN after restart",
              ids(s.req("SELECT id FROM t WHERE id BETWEEN 2 AND 3 ORDER BY id")) == [2, 3])
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
