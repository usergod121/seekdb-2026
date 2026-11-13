#!/usr/bin/env python3
"""Profiles the query path: loads a table, runs TopK queries, then prints the engine's stage profile.

Usage: profile_query.py [--binary PATH] [--rows N] [--dims N] [--queries N] [--filtered half]
"""
import argparse
import json
import os
import random
import statistics
import subprocess
import tempfile
import time


def fmt(vec):
    return ",".join("%.4f" % v for v in vec)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default="/mnt/e/mvdb/build/minivectordb")
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--dims", type=int, default=128)
    ap.add_argument("--queries", type=int, default=200)
    ap.add_argument("--filtered", choices=["none", "half", "all"], default="half")
    args = ap.parse_args()

    rng = random.Random(11)
    vectors = [[round(rng.uniform(-1, 1), 4) for _ in range(args.dims)] for _ in range(args.rows)]
    probes = [[round(rng.uniform(-1, 1), 4) for _ in range(args.dims)] for _ in range(args.queries)]
    data = tempfile.mkdtemp(prefix="mvdb_pq_")
    env = dict(os.environ)
    env["MVDB_PROFILE"] = "1"
    proc = subprocess.Popen([args.binary, "--data-dir", data], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, env=env)
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
        return line()

    line()
    req("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), emb VECTOR(%d))" % args.dims)
    for start in range(0, args.rows, 500):
        values = ",".join("(%d,'c%d','[%s]')" % (i + 1, i % 8, fmt(vectors[i]))
                          for i in range(start, min(start + 500, args.rows)))
        req("INSERT INTO docs VALUES " + values)

    latencies = []
    for index, probe in enumerate(probes):
        use_filter = (args.filtered == "all" or (args.filtered == "half" and index % 2 == 1))
        if use_filter:
            sql = ("SELECT id FROM docs WHERE category='c%d' ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT 10"
                   % (index % 8, fmt(probe)))
        else:
            sql = ("SELECT id FROM docs ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT 10" % fmt(probe))
        started = time.perf_counter()
        raw = req(sql)
        latencies.append((time.perf_counter() - started) * 1e6)
        if raw is None or '"ok":true' not in raw:
            print("query failed: %r" % (raw or "")[:200])
            return 1
    latencies.sort()
    print("queries %d  min %.2f us  median %.2f us" % (len(latencies), latencies[0],
                                                       statistics.median(latencies)))
    seq += 1
    os.write(proc.stdin.fileno(), (json.dumps({"id": seq, "op": "shutdown"}) + "\n").encode())
    line()
    err = proc.stderr.read().decode("utf-8", "replace")
    proc.wait(timeout=120)
    print(err.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
