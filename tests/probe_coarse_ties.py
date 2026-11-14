#!/usr/bin/env python3
"""Does the approximate path survive a large table with many duplicate vectors?

The coarse pass keeps a shortlist; when thousands of rows share the same distance, the true top-k depends on
the primary-key tie-break alone, so the shortlist has to contain the low-pk rows of the tied group. This
probe builds tables with heavy duplication (all rows identical, a few distinct values, and a half-duplicated
column) at sizes above the coarse-path threshold and compares every answer with the exact scan.

Usage: probe_coarse_ties.py [--binary PATH] [--golden PATH]
"""
import argparse
import json
import os
import random
import subprocess
import tempfile


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
        return json.loads(self.line())

    def shutdown(self):
        self.seq += 1
        os.write(self.proc.stdin.fileno(), (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
        self.line()
        self.proc.wait(timeout=180)


SHAPES = [
    ("all rows identical", lambda i: [0.5] * 8),
    ("four distinct vectors", lambda i: [0.25 * (i % 4), -0.5, 0.75, 0.0, 0.125, -0.25, 0.5, 0.25]),
    ("half duplicated, half random", None),
    ("duplicates after update/delete churn", "churn"),
]


def build(binary, data, shape, rows, rng):
    session = Session(binary, data)
    session.req("CREATE TABLE docs (id BIGINT PRIMARY KEY, emb VECTOR(8))")
    for start in range(0, rows, 200):
        values = []
        for i in range(start, min(start + 200, rows)):
            if shape == "churn":
                vec = [0.5] * 8 if i % 3 else [round(rng.uniform(-1, 1), 4) for _ in range(8)]
            elif shape is None:
                vec = [0.5] * 8 if i % 2 == 0 else [round(rng.uniform(-1, 1), 4) for _ in range(8)]
            else:
                vec = shape(i)
            values.append("(%d,'[%s]')" % (i + 1, fmt(vec)))
        reply = session.req("INSERT INTO docs VALUES " + ",".join(values))
        if reply.get("ok") is not True:
            raise SystemExit("insert failed: %r" % (reply,))
    if shape == "churn":
        # Rewrite and remove rows so that the cached order stops matching primary-key order.
        for start in range(1, rows, 7):
            session.req("UPDATE docs SET emb='[0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5]' WHERE id=%d" % start)
        for start in range(3, rows, 5):
            session.req("DELETE FROM docs WHERE id=%d" % start)
        for start in range(2, rows, 11):
            session.req("INSERT INTO docs VALUES (%d,'[%s]')" % (start, fmt([0.5] * 8)))
    return session


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default="/mnt/e/mvdb/build/minivectordb")
    ap.add_argument("--golden", default="/mnt/e/pw/round2/golden/mvdb-gate-proven")
    ap.add_argument("--rows", type=int, default=4096)
    ap.add_argument("--queries", type=int, default=60)
    args = ap.parse_args()

    rng = random.Random(4242)
    problems = 0
    checks = 0
    for label, shape in SHAPES:
        data = tempfile.mkdtemp(prefix="mvdb_ties_")
        fast = build(args.binary, data, shape, args.rows, rng)
        exact = Session(args.golden, data)
        for q in range(args.queries):
            probe = "[%s]" % fmt([0.5] * 8 if q % 2 == 0
                                 else [round(rng.uniform(-1, 1), 4) for _ in range(8)])
            sql = "SELECT id FROM docs ORDER BY L2_DISTANCE(emb,'%s') LIMIT 10" % probe
            got = [row[0] for row in fast.req(sql).get("rows", [])]
            want = [row[0] for row in exact.req(sql).get("rows", [])]
            checks += 1
            if got != want:
                problems += 1
                if problems <= 5:
                    print("MISMATCH [%s] q=%d\n  got  %r\n  want %r" % (label, q, got, want))
        fast.shutdown()
        exact.shutdown()
        print("%-32s rows=%d queries=%d checked" % (label, args.rows, args.queries))
    print("checks: %d  problems: %d" % (checks, problems))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
