#!/usr/bin/env python3
"""Measures the cost of the equality-index boundary.

The cached scan path builds a value -> row index per column and abandons it once a column has more
distinct values than rows/16+64. This probe puts a low-cardinality column (8 values) and a
high-cardinality one (one value per row) in the same table and times three query classes:

  * filtered on the low-cardinality column   -> the index applies, the scan walks 1/8 of the rows
  * filtered on the high-cardinality column  -> the index was abandoned, the scan walks everything
  * unfiltered                               -> the reference full scan

Usage: bench_index_boundary.py --binary <path> [--rows N] [--dims N] [--queries N]
"""
import argparse
import json
import os
import random
import statistics
import subprocess
import tempfile


def fmt(vec):
    return ",".join("%.4f" % v for v in vec)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default="/mnt/e/mvdb/build/minivectordb")
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--dims", type=int, default=128)
    ap.add_argument("--queries", type=int, default=300)
    ap.add_argument("--topk", type=int, default=10)
    args = ap.parse_args()

    rng = random.Random(20261113)
    vectors = [[round(rng.uniform(-1, 1), 4) for _ in range(args.dims)] for _ in range(args.rows)]
    probes = [[round(rng.uniform(-1, 1), 4) for _ in range(args.dims)]
              for _ in range(args.queries)]

    data = tempfile.mkdtemp(prefix="mvdb_ib_")
    proc = subprocess.Popen([args.binary, "--data-dir", data], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    buf = b""
    seq = 0

    def line():
        nonlocal buf
        while b"\n" not in buf:
            chunk = os.read(proc.stdout.fileno(), 1 << 20)
            if not chunk:
                return None
            buf += chunk
        out, _, buf = buf.partition(b"\n")
        return out.decode("utf-8", "replace").rstrip("\r")

    def req(sql):
        nonlocal seq
        seq += 1
        os.write(proc.stdin.fileno(),
                 (json.dumps({"id": seq, "op": "execute", "sql": sql,
                              "search_mode": "exact"}) + "\n").encode())
        return json.loads(line())

    line()
    reply = req("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), "
                "name VARCHAR(16), emb VECTOR(%d))" % args.dims)
    if reply.get("ok") is not True:
        print("create failed: %r" % (reply,))
        return 1
    for start in range(0, args.rows, 500):
        values = ",".join("(%d,'c%d','u%05d','[%s]')" % (i + 1, i % 8, i, fmt(vectors[i]))
                          for i in range(start, min(start + 500, args.rows)))
        reply = req("INSERT INTO docs VALUES " + values)
        if reply.get("ok") is not True:
            print("insert failed: %r" % (reply,))
            return 1

    classes = {"low-cardinality filter": [], "high-cardinality filter": [], "no filter": []}
    for index, probe in enumerate(probes):
        kind = ["low-cardinality filter", "high-cardinality filter", "no filter"][index % 3]
        if kind == "low-cardinality filter":
            sql = ("SELECT id FROM docs WHERE category='c%d' "
                   "ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT %d" % (index % 8, fmt(probe), args.topk))
        elif kind == "high-cardinality filter":
            target = index * 37 % args.rows
            sql = ("SELECT id FROM docs WHERE name='u%05d' "
                   "ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT %d" % (target, fmt(probe), args.topk))
        else:
            sql = ("SELECT id FROM docs ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT %d"
                   % (fmt(probe), args.topk))
        started = time_ns()
        reply = req(sql)
        elapsed = (time_ns() - started) / 1000.0
        if reply.get("ok") is not True:
            print("query failed: %r" % (reply,))
            return 1
        classes[kind].append(elapsed)

    print("rows %d dims %d queries %d k=%d" % (args.rows, args.dims, args.queries, args.topk))
    for kind, values in classes.items():
        values = sorted(values)
        print("  %-26s n=%3d min=%.2f us p10=%.2f us median=%.2f us"
              % (kind, len(values), values[0],
                 values[max(0, len(values) // 10)], statistics.median(values)))
    seq += 1
    os.write(proc.stdin.fileno(), (json.dumps({"id": seq, "op": "shutdown"}) + "\n").encode())
    line()
    proc.wait(timeout=60)
    return 0


def time_ns():
    import time
    return time.perf_counter_ns()


if __name__ == "__main__":
    raise SystemExit(main())
