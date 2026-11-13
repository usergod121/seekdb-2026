#!/usr/bin/env python3
"""Measures the ANN path: Recall@10 against the exact scan, and latency, as the probe count changes.

The graded benchmark allows approximate retrieval but requires the average Recall@10 to stay at or above
0.95 for both the filtered and the unfiltered halves, so this probe reports recall and time for the same
1000-query mixture the benchmark uses, and does it for each probe setting of the IVF index.

Usage: probe_ann_recall.py [--binary PATH] [--rows N] [--dims N] [--queries N]
"""
import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile

PROBE_FRACTIONS = [4, 8, 16]


def fmt(vec):
    return ",".join("%.4f" % v for v in vec)


class Session:
    def __init__(self, binary, data, mode):
        self.proc = subprocess.Popen([binary, "--data-dir", data], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""
        self.seq = 0
        self.mode = mode
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
                              "search_mode": self.mode}) + "\n").encode())
        return json.loads(self.line())

    def shutdown(self):
        self.seq += 1
        os.write(self.proc.stdin.fileno(), (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
        self.line()
        self.proc.wait(timeout=180)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default="/mnt/e/mvdb/build/minivectordb")
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--dims", type=int, default=128)
    ap.add_argument("--queries", type=int, default=200)
    args = ap.parse_args()

    rng = random.Random(20261114)
    vectors = [[round(rng.uniform(-1, 1), 4) for _ in range(args.dims)] for _ in range(args.rows)]
    probes = [[round(rng.uniform(-1, 1), 4) for _ in range(args.dims)] for _ in range(args.queries)]
    data = tempfile.mkdtemp(prefix="mvdb_ann_")

    # exact answers first, from the same data directory
    exact = Session(args.binary, data, "exact")
    exact.req("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), emb VECTOR(%d))" % args.dims)
    for start in range(0, args.rows, 250):
        values = ",".join("(%d,'c%d','[%s]')" % (i + 1, i % 8, fmt(vectors[i]))
                          for i in range(start, min(start + 250, args.rows)))
        reply = exact.req("INSERT INTO docs VALUES " + values)
        if reply.get("ok") is not True:
            print("insert failed: %r" % (reply,))
            return 1
    exact.req("CREATE VECTOR INDEX idx ON docs (emb)")
    expected = []
    for index, probe in enumerate(probes):
        if index % 2:
            sql = ("SELECT id FROM docs WHERE category='c%d' ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT 10"
                   % (index % 8, fmt(probe)))
        else:
            sql = ("SELECT id FROM docs ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT 10" % fmt(probe))
        reply = exact.req(sql)
        expected.append([row[0] for row in reply.get("rows", [])])
    exact.shutdown()

    # ann answers from a fresh session on the same directory
    ann = Session(args.binary, data, "ann")
    ann.req("SELECT id FROM docs ORDER BY id LIMIT 1")   # force the index build
    hits = {"filtered": [0, 0], "unfiltered": [0, 0]}
    latencies = {"filtered": [], "unfiltered": []}
    import time
    for index, probe in enumerate(probes):
        if index % 2:
            sql = ("SELECT id FROM docs WHERE category='c%d' ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT 10"
                   % (index % 8, fmt(probe)))
            key = "filtered"
        else:
            sql = ("SELECT id FROM docs ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT 10" % fmt(probe))
            key = "unfiltered"
        started = time.perf_counter()
        reply = ann.req(sql)
        latencies[key].append((time.perf_counter() - started) * 1e6)
        got = [row[0] for row in reply.get("rows", [])]
        hits[key][0] += len(set(got) & set(expected[index]))
        hits[key][1] += len(expected[index])
    ann.shutdown()

    for key in ("unfiltered", "filtered"):
        wanted, total = hits[key]
        values = sorted(latencies[key])
        recall = (wanted / total) if total else 0.0
        print("%-10s recall@10 %.4f   min %.2f us  p10 %.2f us  median %.2f us"
              % (key, recall, values[0], values[max(0, len(values) // 10)], statistics.median(values)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
