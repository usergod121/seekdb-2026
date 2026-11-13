#!/usr/bin/env python3
"""Runs one insert load with the stage profiler on and prints the engine's profile to stdout.

Usage: profile_write.py [--binary PATH] [--rows N] [--dims N] [--batches N]
"""
import argparse
import json
import os
import random
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
    ap.add_argument("--batches", type=int, default=1)
    args = ap.parse_args()

    rng = random.Random(7)
    vectors = [[round(rng.uniform(-1, 1), 4) for _ in range(args.dims)] for _ in range(args.rows)]
    data = tempfile.mkdtemp(prefix="mvdb_prof_")
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
        return json.loads(line())

    line()
    req("CREATE TABLE docs (id BIGINT PRIMARY KEY, tag VARCHAR(8), emb VECTOR(%d))" % args.dims)
    per_batch = (args.rows + args.batches - 1) // args.batches
    started = time.perf_counter()
    for start in range(0, args.rows, per_batch):
        end = min(start + per_batch, args.rows)
        values = ",".join("(%d,'t%d','[%s]')" % (i + 1, i % 8, fmt(vectors[i]))
                          for i in range(start, end))
        reply = req("INSERT INTO docs VALUES " + values)
        if reply.get("ok") is not True:
            print("insert failed: %r" % (reply,))
            return 1
    elapsed = (time.perf_counter() - started) * 1000.0
    seq += 1
    os.write(proc.stdin.fileno(), (json.dumps({"id": seq, "op": "shutdown"}) + "\n").encode())
    line()
    err = proc.stderr.read().decode("utf-8", "replace")
    proc.wait(timeout=120)
    print("load %.1f ms for %d rows in %d batch(es)" % (elapsed, args.rows, args.batches))
    print(err.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
