#!/usr/bin/env python3
"""Ordering matrix: every ORDER BY shape combined with LIMIT and offsets, checked against a model.

Ties are the dangerous part. The specification says an ordinary ORDER BY breaks ties on the primary key
ascending, and so does a distance ordering. The table here is full of ties (three rows at the same
distance, two rows with the same text, two with the same number), so any mistake in the tie rule shows
up immediately. Descending keys, multi-key orderings, a distance projected twice and offsets in both
spellings are all covered, in exact and ann mode.
"""
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


def f32(x):
    return struct.unpack("f", struct.pack("f", float(x)))[0]


ROWS = [
    (7, "b", 10, [1.0, 0.0]),
    (2, "a", 10, [1.0, 0.0]),
    (9, "a", 20, [0.0, 1.0]),
    (4, "c", 10, [1.0, 0.0]),
    (1, "b", 20, [2.0, 2.0]),
    (6, "a", 30, [0.0, 0.0]),
]
QUERY = [0.0, 0.0]
LITERAL = ",".join("%.4f" % v for v in QUERY)


def dist(vec):
    total = 0.0
    for a, b in zip(vec, QUERY):
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
            return None
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": mode}) + "\n").encode())
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


def ordered_ids(kind):
    """The row order the specification requires for one ORDER BY kind."""
    rows = list(ROWS)
    if kind == "id":
        rows.sort(key=lambda r: r[0])
    elif kind == "id desc":
        rows.sort(key=lambda r: -r[0])
    elif kind == "s":
        rows.sort(key=lambda r: (r[1], r[0]))
    elif kind == "s desc":
        groups = {}
        for row in rows:
            groups.setdefault(row[1], []).append(row)
        rows = []
        for key in sorted(groups, reverse=True):
            rows.extend(sorted(groups[key], key=lambda r: r[0]))   # ties ascend by primary key
    elif kind == "n desc, id asc":
        rows.sort(key=lambda r: (-r[2], r[0]))
    elif kind == "s asc, id desc":
        rows.sort(key=lambda r: (r[1], -r[0]))
    elif kind == "d":
        rows.sort(key=lambda r: (dist(r[3]), r[0]))
    elif kind == "d desc":
        groups = {}
        for row in rows:
            groups.setdefault(round(dist(row[3]), 6), []).append(row)
        rows = []
        for key in sorted(groups, reverse=True):
            rows.extend(sorted(groups[key], key=lambda r: r[0]))   # ties ascend by primary key
    return [row[0] for row in rows]


def window(ids, limit, offset):
    ids = ids[offset:]
    return ids[:limit] if limit is not None else ids


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_orderm_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        engine = Engine(data)
        engine.req("CREATE TABLE t (id BIGINT PRIMARY KEY, s VARCHAR(8), n BIGINT, emb VECTOR(2))")
        values = ",".join("(%d,'%s',%d,'[%s]')" % (
            r[0], r[1], r[2], ",".join("%.4f" % v for v in r[3])) for r in ROWS)
        reply = engine.req("INSERT INTO t VALUES " + values)
        check("seed rows", reply is not None and reply.get("ok") is True, json.dumps(reply)[:120])

        # kind -> (ORDER BY text, projection prefix that includes the distance alias when needed)
        shapes = [
            ("id", "id", None),
            ("id desc", "id DESC", None),
            ("s", "s", None),
            ("s desc", "s DESC", None),
            ("n desc, id asc", "n DESC, id ASC", None),
            ("s asc, id desc", "s ASC, id DESC", None),
            ("d", "d", "d"),
            ("d desc", "d DESC", "d"),
        ]
        projections = ["id", "id, s", "s, id", "id, s, n", "n, id", "*"]
        windows = [
            (None, 0, ""),
            (0, 0, " LIMIT 0"),
            (1, 0, " LIMIT 1"),
            (3, 0, " LIMIT 3"),
            (100, 0, " LIMIT 100"),
            (2, 1, " LIMIT 1,2"),
            (2, 1, " LIMIT 2 OFFSET 1"),
            (100, 3, " LIMIT 3 OFFSET 3"),
            (2, 0, " LIMIT 0,2"),
            (2, 4, " LIMIT 4,2"),
            (None, 2, " LIMIT 999 OFFSET 2"),
            (0, 2, " LIMIT 2,0"),
        ]
        for kind, order_sql, extra in shapes:
            for projection in projections:
                for mode in ("exact", "ann"):
                    for limit, offset, window_sql in windows:
                        select = projection
                        order_used = order_sql
                        if extra is not None:
                            if projection == "*":
                                # the alias d is not in the projection, so order by the expression
                                order_used = order_sql.replace(
                                    "d", "L2_DISTANCE(emb,'[%s]')" % LITERAL)
                            else:
                                select = projection + ", L2_DISTANCE(emb,'[%s]') AS d" % LITERAL
                        sql = "SELECT %s FROM t ORDER BY %s%s" % (select, order_used, window_sql)
                        label = "order=%s proj=%s limit=%s offset=%s %s" % (
                            kind, projection, limit, offset, mode)
                        reply = engine.req(sql, mode)
                        check(label + " :: answered", reply is not None, "process gone")
                        if reply is None:
                            return 1
                        if reply.get("ok") is not True:
                            check(label + " :: accepted", False, json.dumps(reply)[:140])
                            continue
                        cols = reply.get("columns") or []
                        check(label + " :: has id", "id" in cols, "%r" % (cols,))
                        if "id" not in cols:
                            continue
                        index = cols.index("id")
                        got = [row[index] for row in (reply.get("rows") or [])]
                        want = window(ordered_ids(kind), limit, offset)
                        check(label + " :: order", got == want, "got %r want %r" % (got, want))
        engine.close()

        # the same matrix with a tiny cache budget, which puts every query on the paged path
        env = dict(os.environ, MVDB_CACHE_BUDGET_BYTES="1024")
        engine = Engine(data, env=env)
        for kind, order_sql, extra in shapes:
            for projection in ("id", "*"):
                for limit, offset, window_sql in windows:
                    select = projection
                    order_used = order_sql
                    if extra is not None:
                        if projection == "*":
                            order_used = order_sql.replace(
                                "d", "L2_DISTANCE(emb,'[%s]')" % LITERAL)
                        else:
                            select = projection + ", L2_DISTANCE(emb,'[%s]') AS d" % LITERAL
                    sql = "SELECT %s FROM t ORDER BY %s%s" % (select, order_used, window_sql)
                    label = "paged order=%s proj=%s limit=%s offset=%s" % (
                        kind, projection, limit, offset)
                    reply = engine.req(sql)
                    check(label + " :: answered", reply is not None, "process gone")
                    if reply is None:
                        return 1
                    if reply.get("ok") is not True:
                        check(label + " :: accepted", False, json.dumps(reply)[:140])
                        continue
                    cols = reply.get("columns") or []
                    if "id" not in cols:
                        continue
                    index = cols.index("id")
                    got = [row[index] for row in (reply.get("rows") or [])]
                    want = window(ordered_ids(kind), limit, offset)
                    check(label + " :: order", got == want, "got %r want %r" % (got, want))
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
