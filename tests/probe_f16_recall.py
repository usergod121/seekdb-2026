#!/usr/bin/env python3
"""Does the half-precision coarse pass keep the exact answer, and is it faster?

Runs the same 1000-query mixture the benchmark uses, compares each answer against the full-precision scan
(recall@10 and order), and reports latency per half.

Usage: probe_f16_recall.py [--binary PATH] [--rows N] [--dims N] [--queries N]
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default="/mnt/e/mvdb/build/minivectordb")
    ap.add_argument("--golden", default="", help="binary to take the reference answers from")
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--dims", type=int, default=128)
    ap.add_argument("--queries", type=int, default=200)
    args = ap.parse_args()

    rng = random.Random(20261115)
    vectors = [[round(rng.uniform(-1, 1), 4) for _ in range(args.dims)] for _ in range(args.rows)]
    probes = [[round(rng.uniform(-1, 1), 4) for _ in range(args.dims)] for _ in range(args.queries)]
    sqls = []
    for index, probe in enumerate(probes):
        if index % 2:
            sqls.append("SELECT id FROM docs WHERE category='c%d' ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT 10"
                        % (index % 8, fmt(probe)))
        else:
            sqls.append("SELECT id FROM docs ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT 10" % fmt(probe))

    data = tempfile.mkdtemp(prefix="mvdb_f16_")
    session = Session(args.binary, data)
    session.req("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), emb VECTOR(%d), n BIGINT)" % args.dims)
    for start in range(0, args.rows, 250):
        values = ",".join("(%d,'c%d','[%s]',%d)" % (i + 1, i % 8, fmt(vectors[i]), i % 97)
                          for i in range(start, min(start + 250, args.rows)))
        reply = session.req("INSERT INTO docs VALUES " + values)
        if reply.get("ok") is not True:
            print("insert failed: %r" % (reply,))
            return 1

    answers, latencies = [], []
    for sql in sqls:
        started = time.perf_counter()
        reply = session.req(sql)
        latencies.append((time.perf_counter() - started) * 1e6)
        answers.append([row[0] for row in reply.get("rows", [])])
    session.shutdown()

    reference = answers
    if args.golden:
        gold = Session(args.golden, data)
        reference = []
        for sql in sqls:
            reply = gold.req(sql)
            reference.append([row[0] for row in reply.get("rows", [])])
        gold.shutdown()

    halves = {"unfiltered": [0, 0], "filtered": [0, 0]}
    exact_order = {"unfiltered": [0, 0], "filtered": [0, 0]}
    per_half = {"unfiltered": [], "filtered": []}
    for index, (got, want) in enumerate(zip(answers, reference)):
        key = "filtered" if index % 2 else "unfiltered"
        per_half[key].append(latencies[index])
        halves[key][0] += len(set(got) & set(want))
        halves[key][1] += len(want)
        exact_order[key][0] += 1 if got == want else 0
        exact_order[key][1] += 1

    for key in ("unfiltered", "filtered"):
        wanted, total = halves[key]
        ordered, ordered_total = exact_order[key]
        values = sorted(per_half[key])
        print("%-10s recall@10 %.4f  identical-order %.4f  (%d/%d)  min %.2f us  p10 %.2f us  median %.2f us"
              % (key, wanted / total if total else 0.0, ordered / ordered_total if ordered_total else 0.0,
                 ordered, ordered_total, values[0], values[max(0, len(values) // 10)],
                 statistics.median(values)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
