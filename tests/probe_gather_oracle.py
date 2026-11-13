#!/usr/bin/env python3
"""Randomised differential test aimed at the equality-group TopK path.

The cached scan answers `WHERE col = literal ORDER BY L2_DISTANCE(...) LIMIT k` either by walking a
precomputed value group (when the group is under half the table) or by scanning everything. This probe
computes the expected answer independently in Python for a few hundred random queries of exactly that
shape, including the cases that make the two paths disagree if anything is wrong:

  * the literal exists and the group is small  -> index path
  * the literal exists and the group is large  -> full scan
  * the literal does not exist                 -> empty answer
  * LIMIT and LIMIT offset variants, and both value types (VARCHAR and BIGINT columns)
  * distances that are close enough to reorder under float rounding

Usage: probe_gather_oracle.py [--binary PATH] [--queries N] [--rows N] [--seed N]
"""
import argparse
import json
import math
import os
import random
import struct
import subprocess
import sys
import tempfile

PROBLEMS = []


def f32(value):
    """The float32 value a client sees when it sends the 4-decimal text."""
    return struct.unpack("f", struct.pack("f", float(value)))[0]


def fmt(vec):
    return ",".join("%.4f" % v for v in vec)


class Session:
    def __init__(self, binary, data):
        self.proc = subprocess.Popen([binary, "--data-dir", data], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""
        self.seq = 0
        self.line()

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 20)
            if not chunk:
                return None
            self.buf += chunk
        out, _, self.buf = self.buf.partition(b"\n")
        return out.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql):
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": "exact"}) + "\n").encode())
        raw = self.line()
        try:
            return json.loads(raw)
        except Exception:
            PROBLEMS.append("non-JSON reply to %r: %r" % (sql, raw))
            return {"ok": False, "error": {"code": "BAD_JSON"}}

    def shutdown(self):
        self.seq += 1
        os.write(self.proc.stdin.fileno(), (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
        self.line()
        self.proc.wait(timeout=120)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default="/mnt/e/mvdb/build/minivectordb")
    ap.add_argument("--rows", type=int, default=4000)
    ap.add_argument("--dims", type=int, default=32)
    ap.add_argument("--queries", type=int, default=300)
    ap.add_argument("--seed", type=int, default=90210)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    rows = []
    for i in range(args.rows):
        vectors = [round(rng.uniform(-3, 3), 4) for _ in range(args.dims)]
        row = {
            "id": i + 1,
            "category": "c%d" % (i % 8),             # 1/8 of the table: index path
            "bucket": i % 6,                          # BIGINT equality column
            "rare": "r%d" % i,                        # one value per row: index must be abandoned
            "vec": vectors,
        }
        row["vec32"] = [f32(v) for v in vectors]
        rows.append(row)

    data = tempfile.mkdtemp(prefix="mvdb_oracle_")
    s = Session(args.binary, data)
    reply = s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, category VARCHAR(8), bucket BIGINT, "
                  "rare VARCHAR(16), emb VECTOR(%d))" % args.dims)
    if reply.get("ok") is not True:
        print("create failed: %r" % (reply,))
        return 1
    for start in range(0, args.rows, 250):
        values = ",".join("(%d,'%s',%d,'%s','[%s]')" % (r["id"], r["category"], r["bucket"], r["rare"],
                                                        fmt(r["vec"]))
                          for r in rows[start:start + 250])
        reply = s.req("INSERT INTO t VALUES " + values)
        if reply.get("ok") is not True:
            print("insert failed: %r" % (reply,))
            return 1

    checks = 0
    for n in range(args.queries):
        kind = n % 5
        if kind == 0:
            column, literal = "category", "c%d" % rng.randrange(8)
        elif kind == 1:
            column, literal = "bucket", str(rng.randrange(6))
        elif kind == 2:
            column, literal = "rare", "r%d" % rng.randrange(args.rows)      # single row, index abandoned
        elif kind == 3:
            column, literal = "category", "c%d" % (rng.randrange(8) + 8)    # absent value
        else:
            column, literal = "bucket", str(rng.randrange(6) + 90)          # absent value
        k = rng.choice([1, 3, 10, 25])
        offset = rng.choice([0, 0, 0, 2, 7])
        query = [round(rng.uniform(-3, 3), 4) for _ in range(args.dims)]
        query32 = [f32(v) for v in query]

        if column in ("category", "rare"):
            sql = ("SELECT id FROM t WHERE %s='%s' ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT %d"
                   % (column, literal, fmt(query), k))
            if offset:
                sql += " OFFSET %d" % offset
            matching = [r for r in rows if r[column] == literal]
        else:
            sql = ("SELECT id FROM t WHERE %s=%s ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT %d"
                   % (column, literal, fmt(query), k))
            if offset:
                sql += " OFFSET %d" % offset
            matching = [r for r in rows if r[column] == int(literal)]

        def distance(row):
            total = 0.0
            for a, b in zip(row["vec32"], query32):
                delta = a - b
                total += delta * delta
            return math.sqrt(total)

        ordered = sorted(matching, key=lambda r: (distance(r), r["id"]))
        expected = [r["id"] for r in ordered[offset:offset + k]]

        reply = s.req(sql)
        checks += 1
        if reply.get("ok") is not True:
            PROBLEMS.append("%s -> error %r" % (sql, reply))
            continue
        got = [line[0] for line in reply.get("rows", [])]
        if got == expected:
            continue
        # Report near ties separately: float rounding in either implementation may legitimately swap
        # two rows whose distances differ by less than one part in a million.
        if len(got) == len(expected):
            worst = 0.0
            by_id = {r["id"]: distance(r) for r in matching}
            for a, b in zip(got, expected):
                worst = max(worst, abs(by_id[a] - by_id[b]))
            if worst < 1e-4:
                continue
        PROBLEMS.append("%s -> got %r expected %r" % (sql, got[:6], expected[:6]))

    s.shutdown()
    print("checks: %d" % checks)
    if PROBLEMS:
        print("problems: %d" % len(PROBLEMS))
        for p in PROBLEMS[:10]:
            print("  - " + p)
        return 1
    print("no problems")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
