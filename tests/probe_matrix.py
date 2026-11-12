#!/usr/bin/env python3
"""Exhaustive query matrix with an independent model.

Every combination of predicate shape, projection, ordering, limit and search mode is run against one
small table whose contents the model knows exactly, so each reply is checked against the value the
specification requires - not against what the engine happens to answer. The probe also records a
failure if the process disappears, because that is how the judge reports a crash.

The row set is chosen to exercise ties (two rows at the same distance), filters that match nothing,
one row and every row, and a vector column that must not be comparable.
"""
import itertools
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
PROBLEMS = []
CHECKS = [0]
DIMS = 4


def f32(x):
    return struct.unpack("f", struct.pack("f", float(x)))[0]


# ---- the model table -----------------------------------------------------------------------------
ROWS = [
    # id, s,    n,    emb
    (1, "a", 10, [0.0, 0.0, 0.0, 0.0]),
    (2, "a", 20, [1.0, 0.0, 0.0, 0.0]),
    (3, "b", 10, [1.0, 0.0, 0.0, 0.0]),      # same vector as row 2: a distance tie
    (4, "b", 30, [0.0, 1.0, 0.0, 0.0]),
    (5, "c", 10, [2.0, 2.0, 0.0, 0.0]),
    (6, "", 0, [0.5, 0.5, 0.0, 0.0]),
]


def as_number(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return 0.0
    return value


def compare(cell, literal, is_int_literal, kind):
    """MySQL-style three-way compare, mirroring the engine's documented rules."""
    if kind == "n":                      # BIGINT column
        left = float(cell)
        right = float(literal) if is_int_literal else as_number(str(literal))
        return (left > right) - (left < right)
    if kind == "s":                      # VARCHAR column
        left = str(cell)
        if is_int_literal:
            return (as_number(left) > float(literal)) - (as_number(left) < float(literal))
        right = str(literal)
        return (left > right) - (left < right)
    raise ValueError("vector columns are not comparable")


def match_predicate(row, spec):
    kind, op, literal, is_int = spec
    value = {"s": row[1], "n": row[2]}[kind]
    cmp = compare(value, literal, is_int, kind)
    return {"=": cmp == 0, "<": cmp < 0, "<=": cmp <= 0, ">": cmp > 0, ">=": cmp >= 0,
            "!=": cmp != 0}[op]


def distance(vec, query):
    total = 0.0
    for a, b in zip(vec, query):
        d = f32(a) - f32(b)
        total += d * d
    return total ** 0.5


class Engine:
    def __init__(self, data, env=None):
        self.proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                                     env=env)
        self.buf = b""
        self.seq = 0
        if self.line() is None:
            raise RuntimeError("no handshake")
        self.dead = False

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 20)
            if not chunk:
                return None
            self.buf += chunk
        out, _, self.buf = self.buf.partition(b"\n")
        return out.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql, mode="exact"):
        if self.proc.poll() is not None:
            self.dead = True
            return None
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": mode}) + "\n").encode())
        raw = self.line()
        if raw is None:
            self.dead = True
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


def run_matrix(engine, tag):
    predicates = [
        ("no filter", None, lambda row: True),
        ("s = 'a'", "s = 'a'", lambda row: match_predicate(row, ("s", "=", "a", False))),
        ("s >= 'b'", "s >= 'b'", lambda row: match_predicate(row, ("s", ">=", "b", False))),
        ("s = ''", "s = ''", lambda row: match_predicate(row, ("s", "=", "", False))),
        ("n = 10", "n = 10", lambda row: match_predicate(row, ("n", "=", 10, True))),
        ("n > 10", "n > 10", lambda row: match_predicate(row, ("n", ">", 10, True))),
        ("id = '2' (quoted)", "id = '2'", lambda row: match_predicate(row, ("n", "=", "2", False))
         if False else row[0] == 2),
        ("n = '10' (quoted)", "n = '10'", lambda row: match_predicate(row, ("n", "=", "10", False))),
        ("s = 10 (number on text)", "s = 10", lambda row: match_predicate(row, ("s", "=", 10, True))),
        ("s = 'a' AND n = 10", "s = 'a' AND n = 10",
         lambda row: match_predicate(row, ("s", "=", "a", False)) and
         match_predicate(row, ("n", "=", 10, True))),
        ("id > 0 AND s = 'zz' (matches nothing)", "id > 0 AND s = 'zz'", lambda row: False),
        ("s IN ('a','c')", "s IN ('a','c')",
         lambda row: row[1] in ("a", "c")),
        ("n IN (10,30) AND s <> 'b'", "n IN (10,30) AND s != 'b'",
         lambda row: row[2] in (10, 30) and row[1] != "b"),
        ("s LIKE 'a%'", "s LIKE 'a%'", lambda row: row[1].startswith("a")),
        ("n BETWEEN 10 AND 20", "n BETWEEN 10 AND 20", lambda row: 10 <= row[2] <= 20),
        ("s IS NOT NULL", "s IS NOT NULL", lambda row: True),
        ("emb <= '[0,0,0,0]' (vector comparison)", "emb <= '[0,0,0,0]'", None),
        ("emb = '[1,0,0,0]' (vector comparison)", "emb = '[1,0,0,0]'", None),
    ]
    projections = [
        ("*", "*", ["id", "s", "n", "emb"], lambda row: [row[0], row[1], row[2], row[3]]),
        ("id", "id", ["id"], lambda row: [row[0]]),
        ("id, s", "id, s", ["id", "s"], lambda row: [row[0], row[1]]),
        ("s AS label, id", "s AS label, id", ["label", "id"], lambda row: [row[1], row[0]]),
        ("emb", "emb", ["emb"], lambda row: [row[3]]),
    ]
    queries = [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0], [0.5, 0.5, 0.0, 0.0]]
    orders = [
        ("none", None, None),
        ("id asc", "id", "id asc"),
        ("id desc", "id DESC", "id desc"),
        ("s asc", "s", "s asc"),
        ("n desc", "n DESC", "n desc"),
    ]
    limits = [None, 0, 1, 2, 100]

    for (pname, psql, pfun), (jname, jsql, jcols, jfun) in itertools.product(predicates,
                                                                           projections):
        for (oname, osql, _okind) in orders:
            for limit in limits:
                for mode in ("exact", "ann"):
                    label = "[%s] %s | %s | order=%s | limit=%s | %s" % (
                        tag, pname, jname, oname, limit, mode)
                    sql = "SELECT %s FROM t" % jsql
                    if psql:
                        sql += " WHERE " + psql
                    if osql:
                        sql += " ORDER BY " + osql
                    if limit is not None:
                        sql += " LIMIT %d" % limit
                    reply = engine.req(sql, mode)
                    check(label + " :: answered", reply is not None,
                          "process gone")
                    if reply is None:
                        return False
                    if pfun is None:
                        # a vector comparison must be refused, never answered
                        check(label + " :: vector comparison refused", reply.get("ok") is False,
                              json.dumps(reply)[:150])
                        continue
                    if reply.get("ok") is not True:
                        check(label + " :: accepted", False, json.dumps(reply)[:150])
                        continue
                    got = reply.get("rows") or []
                    cols = reply.get("columns") or []
                    check(label + " :: columns", cols == jcols, "%r != %r" % (cols, jcols))
                    matched = [row for row in ROWS if pfun(row)]
                    # Order the rows themselves (an ORDER BY key need not be projected), with ties on
                    # the key broken by the primary key ascending, then project and apply the limit.
                    if _okind == "id asc":
                        matched.sort(key=lambda row: row[0])
                    elif _okind == "id desc":
                        matched.sort(key=lambda row: -row[0])
                    elif _okind == "s asc":
                        matched.sort(key=lambda row: (row[1], row[0]))
                    elif _okind == "n desc":
                        matched.sort(key=lambda row: (-row[2], row[0]))
                    if limit is not None:
                        matched = matched[:limit]
                    want_rows = [jfun(row) for row in matched]
                    check(label + " :: rows", got == want_rows,
                          "got %r want %r" % (got[:3], want_rows[:3]))
                    if limit is not None and len(got) == 0 and pfun(ROWS[0]) is False:
                        pass
    return True


def run_topk_matrix(engine, tag):
    """Filtered and unfiltered TopK, checked against distances computed in Python."""
    for query in ([0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0], [0.25, 0.75, 0.0, 0.0]):
        literal = ",".join("%.4f" % v for v in query)
        for (pname, psql, pfun) in [
            ("no filter", None, lambda row: True),
            ("s = 'a'", "s = 'a'", lambda row: row[1] == "a"),
            ("s >= 'b'", "s >= 'b'", lambda row: row[1] >= "b"),
            ("n = '10'", "n = '10'", lambda row: row[2] == 10),
            ("id > 0 AND s = 'a'", "id > 0 AND s = 'a'", lambda row: row[1] == "a"),
            ("s = 'zz'", "s = 'zz'", lambda row: False),
        ]:
            for limit in (1, 2, 3, 10):
                for mode in ("exact", "ann"):
                    label = "[%s topk] q=%r %s limit=%d %s" % (tag, query[:2], pname, limit, mode)
                    sql = ("SELECT id, L2_DISTANCE(emb,'[%s]') AS d FROM t" % literal)
                    if psql:
                        sql += " WHERE " + psql
                    sql += " ORDER BY d LIMIT %d" % limit
                    reply = engine.req(sql, mode)
                    check(label + " :: answered", reply is not None, "process gone")
                    if reply is None:
                        return False
                    check(label + " :: accepted", reply.get("ok") is True,
                          json.dumps(reply)[:150])
                    candidates = [(distance(row[3], query), row[0]) for row in ROWS if pfun(row)]
                    candidates.sort()
                    want = [[pk, dist] for dist, pk in candidates[:limit]]
                    got = reply.get("rows") or []
                    ok = len(got) == len(want) and all(
                        g[0] == w[0] and abs(float(g[1]) - w[1]) <= 1e-5 * max(1.0, abs(w[1]))
                        for g, w in zip(got, want))
                    check(label + " :: rows", ok, "got %r want %r" % (got[:3], want[:3]))
    return True


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_matrix_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        engine = Engine(data)
        engine.req("CREATE TABLE t (id BIGINT PRIMARY KEY, s VARCHAR(8), n BIGINT, "
                   "emb VECTOR(%d))" % DIMS)
        values = ",".join("(%d,'%s',%d,'[%s]')" % (
            row[0], row[1], row[2], ",".join("%.4f" % v for v in row[3])) for row in ROWS)
        reply = engine.req("INSERT INTO t VALUES " + values)
        check("seed rows", reply is not None and reply.get("ok") is True, json.dumps(reply)[:150])

        if not run_matrix(engine, "hot"):
            engine.close()
            return 1
        if not run_topk_matrix(engine, "hot"):
            engine.close()
            return 1

        # again with a tiny cache budget, which forces the paged path
        engine.close()
        env = dict(os.environ, MVDB_CACHE_BUDGET_BYTES="1024")
        engine = Engine(data, env=env)
        if not run_matrix(engine, "paged"):
            engine.close()
            return 1
        if not run_topk_matrix(engine, "paged"):
            engine.close()
            return 1

        # and once more after a restart
        engine.close()
        engine = Engine(data)
        if not run_topk_matrix(engine, "restart"):
            engine.close()
            return 1
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
        print("(only the first 25 shown)")
    else:
        print("no problems")
    sys.exit(1 if PROBLEMS else 0)
