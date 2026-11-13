#!/usr/bin/env python3
"""Splits the write phase into device flushes and our own work.

Loads the same 10000 rows in a varying number of committed batches. The commit count only changes the
number of device flushes, so a straight line through the measurements gives the flush cost per commit
(slope) and the cost of the work that does not depend on batching (intercept). That tells us which
half to optimise -- on the judging machine the flush may be far cheaper than on a VM.

Usage: bench_write_split.py [--binary PATH] [--rows N] [--dims N] [--batches 1,4,20,100]
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


def run(binary, rows, dims, batches, rng_seed):
    rng = random.Random(rng_seed)
    vectors = [[round(rng.uniform(-1, 1), 4) for _ in range(dims)] for _ in range(rows)]
    data = tempfile.mkdtemp(prefix="mvdb_wsplit_")
    proc = subprocess.Popen([binary, "--data-dir", data], stdin=subprocess.PIPE,
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
    reply = req("CREATE TABLE docs (id BIGINT PRIMARY KEY, tag VARCHAR(8), emb VECTOR(%d))" % dims)
    if reply.get("ok") is not True:
        return None

    per_batch = (rows + batches - 1) // batches
    started = time.perf_counter()
    for start in range(0, rows, per_batch):
        end = min(start + per_batch, rows)
        values = ",".join("(%d,'t%d','[%s]')" % (i + 1, i % 8, fmt(vectors[i])) for i in range(start, end))
        reply = req("INSERT INTO docs VALUES " + values)
        if reply.get("ok") is not True:
            print("insert failed: %r" % (reply,))
            return None
    elapsed = (time.perf_counter() - started) * 1000.0
    seq += 1
    os.write(proc.stdin.fileno(), (json.dumps({"id": seq, "op": "shutdown"}) + "\n").encode())
    line()
    proc.wait(timeout=120)
    return elapsed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default="/mnt/e/mvdb/build/minivectordb")
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--dims", type=int, default=128)
    ap.add_argument("--batches", default="1,4,20,100")
    ap.add_argument("--repeat", type=int, default=2)
    args = ap.parse_args()

    counts = [int(x) for x in args.batches.split(",")]
    points = []
    for batches in counts:
        samples = [run(args.binary, args.rows, args.dims, batches, 7) for _ in range(args.repeat)]
        samples = [s for s in samples if s is not None]
        if not samples:
            print("batch count %d failed" % batches)
            continue
        median = statistics.median(samples)
        points.append((batches, median))
        print("batches %4d  median %8.1f ms   samples %s"
              % (batches, median, " ".join("%.1f" % s for s in samples)))

    if len(points) >= 2:
        (b1, t1), (b2, t2) = points[0], points[-1]
        slope = (t2 - t1) / (b2 - b1)
        intercept = t1 - slope * b1
        print("flush per commit: %.2f ms   batching-independent work: %.1f ms (%.2f us/row)"
              % (slope, intercept, intercept * 1000.0 / args.rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
