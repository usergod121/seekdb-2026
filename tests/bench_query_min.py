#!/usr/bin/env python3
"""Per-query latency with less noise: minimum and low percentiles.

The benchmark's median jumps around by 15% on this VM, which hides small wins. The minimum is a much
steadier proxy for the kernel cost, so a change can be judged with a handful of runs.

Usage: bench_query_min.py --binary <path> [--rows N] [--dims N] [--queries N] [--filtered share]
"""
import argparse
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


def fmt(vec):
    return ",".join("%.4f" % v for v in vec)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default="/mnt/e/mvdb/build/minivectordb")
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--dims", type=int, default=128)
    ap.add_argument("--queries", type=int, default=500)
    ap.add_argument("--topk", type=int, default=10)
    ap.add_argument("--filtered", choices=["none", "half", "all"], default="half")
    args = ap.parse_args()

    rng = random.Random(20261112)
    vectors = [[round(rng.uniform(-1, 1), 4) for _ in range(args.dims)] for _ in range(args.rows)]
    probes = [[round(rng.uniform(-1, 1), 4) for _ in range(args.dims)] for _ in range(args.queries)]
    data = tempfile.mkdtemp(prefix="mvdb_qmin_")
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
    req("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), emb VECTOR(%d))" % args.dims)
    for start in range(0, args.rows, 500):
        values = ",".join("(%d,'c%d','[%s]')" % (i + 1, i % 8, fmt(vectors[i]))
                          for i in range(start, min(start + 500, args.rows)))
        reply = req("INSERT INTO docs VALUES " + values)
        if reply.get("ok") is not True:
            print("insert failed: %r" % (reply,))
            return 1
    latencies = []
    for index, probe in enumerate(probes):
        use_filter = (args.filtered == "all" or
                      (args.filtered == "half" and index % 2 == 1))
        if use_filter:
            sql = ("SELECT id FROM docs WHERE category='c%d' "
                   "ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT %d" % (index % 8, fmt(probe), args.topk))
        else:
            sql = ("SELECT id FROM docs ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT %d"
                   % (fmt(probe), args.topk))
        begin = time.perf_counter()
        reply = req(sql)
        latencies.append((time.perf_counter() - begin) * 1e6)
        if reply.get("ok") is not True:
            print("query failed: %r" % (reply,))
            return 1
    proc.kill()
    shutil.rmtree(data, ignore_errors=True)
    ordered = sorted(latencies)
    out = {
        "binary": os.path.basename(args.binary),
        "rows": args.rows, "dims": args.dims, "queries": args.queries,
        "filtered": args.filtered,
        "min_us": round(ordered[0], 2),
        "p10_us": round(ordered[len(ordered) // 10], 2),
        "median_us": round(statistics.median(ordered), 2),
        "mean_us": round(statistics.mean(ordered), 2),
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
