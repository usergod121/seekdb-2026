#!/usr/bin/env python3
"""Write-side matrix: the same predicate shapes for UPDATE and DELETE, with an independent model.

Every case recreates the table from a known state, runs one modifying statement (or a transaction that
is then committed or rolled back), and checks both the reported affected_rows and the whole resulting
table. The predicate list mirrors the read matrix, including the comparisons that need coercion and the
ones that must be refused.
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

BASE = [
    (1, "a", 10, [0.0, 0.0, 0.0, 0.0]),
    (2, "a", 20, [1.0, 0.0, 0.0, 0.0]),
    (3, "b", 10, [1.0, 0.0, 0.0, 0.0]),
    (4, "b", 30, [0.0, 1.0, 0.0, 0.0]),
    (5, "c", 10, [2.0, 2.0, 0.0, 0.0]),
    (6, "", 0, [0.5, 0.5, 0.0, 0.0]),
]


def as_number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


PREDICATES = [
    # label, sql, python predicate over (id, s, n, emb)
    ("no filter", None, lambda row: True),
    ("s = 'a'", "s = 'a'", lambda row: row[1] == "a"),
    ("s >= 'b'", "s >= 'b'", lambda row: row[1] >= "b"),
    ("s = ''", "s = ''", lambda row: row[1] == ""),
    ("n = 10", "n = 10", lambda row: row[2] == 10),
    ("n > 10", "n > 10", lambda row: row[2] > 10),
    ("id = '2' (quoted)", "id = '2'", lambda row: row[0] == 2),
    ("n = '10' (quoted)", "n = '10'", lambda row: row[2] == 10),
    ("s = 10 (number on text)", "s = 10", lambda row: as_number(row[1]) == 10),
    ("s = 'a' AND n = 10", "s = 'a' AND n = 10",
     lambda row: row[1] == "a" and row[2] == 10),
    ("s IN ('a','c')", "s IN ('a','c')", lambda row: row[1] in ("a", "c")),
    ("n IN (10,30) AND s != 'b'", "n IN (10,30) AND s != 'b'",
     lambda row: row[2] in (10, 30) and row[1] != "b"),
    ("s LIKE 'a%'", "s LIKE 'a%'", lambda row: row[1].startswith("a")),
    ("n BETWEEN 10 AND 20", "n BETWEEN 10 AND 20", lambda row: 10 <= row[2] <= 20),
    ("s = 'zz' (matches nothing)", "s = 'zz'", lambda row: False),
]


class Engine:
    def __init__(self, data, env=None):
        self.proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                                     env=env)
        self.buf = b""
        self.seq = 0
        if self.line() is None:
            raise RuntimeError("no handshake")

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 20)
            if not chunk:
                return None
            self.buf += chunk
        out, _, self.buf = self.buf.partition(b"\n")
        return out.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql):
        if self.proc.poll() is not None:
            return None
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql}) + "\n").encode())
        raw = self.line()
        if raw is None:
            return None
        return json.loads(raw)

    def close(self):
        if self.proc.poll() is None:
            try:
                self.seq += 1
                os.write(self.proc.stdin.fileno(),
                         (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
                self.line()
                self.proc.wait(timeout=60)
            except Exception:
                self.proc.kill()


def check(label, condition, detail=""):
    CHECKS[0] += 1
    if not condition:
        PROBLEMS.append(label + ("  " + detail if detail else ""))
        print("FAIL  " + label + ("  " + detail if detail else ""))


def seed(engine):
    engine.req("DROP TABLE IF EXISTS t")
    engine.req("CREATE TABLE t (id BIGINT PRIMARY KEY, s VARCHAR(8), n BIGINT, emb VECTOR(4))")
    values = ",".join("(%d,'%s',%d,'[%s]')" % (
        row[0], row[1], row[2], ",".join("%.4f" % v for v in row[3])) for row in BASE)
    reply = engine.req("INSERT INTO t VALUES " + values)
    return reply is not None and reply.get("ok") is True


def table_state(engine):
    reply = engine.req("SELECT id, s, n FROM t ORDER BY id")
    if reply is None or reply.get("ok") is not True:
        return None
    return [tuple(row) for row in reply["rows"]]


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_wmatrix_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        engine = Engine(data)
        for label, psql, pfun in PREDICATES:
            where = (" WHERE " + psql) if psql else ""
            # --- UPDATE one column
            check("seed for " + label, seed(engine), "seed failed")
            want_rows = [row for row in BASE if pfun(row)]
            want = sorted((row[0], "zz", row[2]) for row in want_rows) + \
                sorted((row[0], row[1], row[2]) for row in BASE if not pfun(row))
            reply = engine.req("UPDATE t SET s = 'zz'" + where)
            check("[update s] %s :: answered" % label, reply is not None, "process gone")
            if reply is None:
                return 1
            check("[update s] %s :: affected" % label,
                  reply.get("ok") is True and reply.get("affected_rows") == len(want_rows),
                  json.dumps(reply)[:150])
            check("[update s] %s :: state" % label, table_state(engine) == sorted(want),
                  "%r" % (table_state(engine)[:3],))

            # --- UPDATE two columns (one of them the vector)
            check("seed for " + label, seed(engine), "seed failed")
            reply = engine.req("UPDATE t SET n = 7, emb = '[0.25,0.25,0,0]'" + where)
            check("[update n,emb] %s :: answered" % label, reply is not None, "process gone")
            if reply is None:
                return 1
            check("[update n,emb] %s :: affected" % label,
                  reply.get("ok") is True and reply.get("affected_rows") == len(want_rows),
                  json.dumps(reply)[:150])

            # --- DELETE, then a transaction that rolls it back
            check("seed for " + label, seed(engine), "seed failed")
            reply = engine.req("DELETE FROM t" + where)
            check("[delete] %s :: answered" % label, reply is not None, "process gone")
            if reply is None:
                return 1
            check("[delete] %s :: affected" % label,
                  reply.get("ok") is True and reply.get("affected_rows") == len(want_rows),
                  json.dumps(reply)[:150])
            want_after = sorted((row[0], row[1], row[2]) for row in BASE if not pfun(row))
            check("[delete] %s :: state" % label, table_state(engine) == want_after,
                  "%r" % (table_state(engine)[:3],))

            check("seed for " + label, seed(engine), "seed failed")
            engine.req("BEGIN")
            engine.req("DELETE FROM t" + where)
            engine.req("ROLLBACK")
            want_all = sorted((row[0], row[1], row[2]) for row in BASE)
            check("[delete rollback] %s :: state" % label, table_state(engine) == want_all,
                  "%r" % (table_state(engine)[:3],))

        # a comparison on the vector column must be refused for all three statements
        for statement in ["UPDATE t SET s = 'q'", "DELETE FROM t"]:
            for predicate in ["emb = '[1,0,0,0]'", "emb < '[0.5,0.5,0,0]'"]:
                check("seed before vector comparison", seed(engine), "seed failed")
                reply = engine.req("%s WHERE %s" % (statement, predicate))
                check("[%s] vector comparison refused" % statement.split()[0], reply is not None,
                      "process gone")
                if reply is None:
                    return 1
                check("[%s] vector comparison %r rejected" % (statement.split()[0], predicate),
                      reply.get("ok") is False, json.dumps(reply)[:150])
                check("[%s] nothing changed" % statement.split()[0],
                      table_state(engine) == sorted((r[0], r[1], r[2]) for r in BASE),
                      "%r" % (table_state(engine)[:3],))
        engine.close()
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
    print()
    print("checks: %d" % CHECKS[0])
    if PROBLEMS:
        print("problems: %d" % len(PROBLEMS))
        for item in PROBLEMS[:25]:
            print("  - " + item)
    else:
        print("no problems")
    sys.exit(1 if PROBLEMS else 0)
