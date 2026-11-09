#!/usr/bin/env python3
"""Runs the graded workload shape end to end and checks the answers, not just the timing.

Phase 1 streams the inserts (100 batches of 100 rows, each in an explicit transaction), phase 2 streams
the 1000 TopK queries, exactly as a driver would, and the queries are then checked against the exact
answer computed here. Reports wall time per phase, the data directory size, and Recall@K, so a fast run
that returns the wrong rows is not mistaken for a good one.
"""
import argparse
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import time

ROWS = 10000
QUERIES = 1000
DIMS = 128
K = 10


def gen(rows, queries, dims, seed=20261109):
    rng = random.Random(seed)
    created = ("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), title VARCHAR(64), "
               "emb VECTOR(%d));" % dims)
    vectors = {}
    batches = []
    rows_sql = []
    for i in range(1, rows + 1):
        vec = [round(rng.uniform(-1.0, 1.0), 4) for _ in range(dims)]
        vectors[i] = vec
        text = ",".join("%.4f" % c for c in vec)
        rows_sql.append((i, i % 8, "(%d,'c%d','doc %d','[%s]')" % (i, i % 8, i, text)))
    for start in range(0, len(rows_sql), 100):
        batches.append([text for _, _, text in rows_sql[start:start + 100]])

    probes = []
    for q in range(queries):
        vec = [round(rng.uniform(-1.0, 1.0), 4) for _ in range(dims)]
        filtered = (q % 2 == 1)
        category = q % 8
        sql = ("SELECT id, L2_DISTANCE(emb,'[%s]') AS d FROM docs %sORDER BY d LIMIT %d;"
               % (",".join("%.4f" % c for c in vec),
                  "WHERE category = 'c%d' " % category if filtered else "", K))
        probes.append((sql, vec, category if filtered else None))
    return created, batches, vectors, probes


class Driver:
    def __init__(self, argv):
        # Unbuffered pipes: a buffered read would block until the requested byte count arrives, which
        # deadlocks against a request/response protocol.
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""
        self.pending = []
        if self._line() != "READY":
            raise RuntimeError("no READY")

    def _line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 16)
            if not chunk:
                raise RuntimeError("engine closed its output")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def send(self, text):
        self.pending.append((text + "\n").encode("utf-8"))

    def flush(self):
        payload = b"".join(self.pending)
        self.pending = []
        view = memoryview(payload)
        fd = self.proc.stdin.fileno()
        while view:
            written = os.write(fd, view)
            view = view[written:]

    def read_ack(self):
        while True:
            line = self._line()
            if line.startswith("OK"):
                return int(line.split()[1]) if " " in line else 0
            if line.startswith("ERROR"):
                raise RuntimeError("statement failed: " + line)
            if line in ("READY", "BYE"):
                continue
            raise RuntimeError("unexpected line while waiting for an ack: %r" % line)

    def read_block(self):
        while True:
            line = self._line()
            if line.startswith("ERROR"):
                raise RuntimeError("query failed: " + line)
            if line.startswith("OK") or line in ("READY", "BYE"):
                continue
            header = line.split("\t") if line else []
            rows = []
            while True:
                inner = self._line()
                if inner == "END":
                    return header, rows
                rows.append(inner.split("\t"))

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def exact_topk(vectors, qvec, limit, category=None):
    scored = []
    for pk, vec in vectors.items():
        if category is not None and pk % 8 != category:
            continue
        total = 0.0
        for a, b in zip(vec, qvec):
            d = a - b
            total += d * d
        scored.append((total, pk))
    scored.sort()
    return [(pk, dist ** 0.5) for dist, pk in scored[:limit]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--docker", default=None, help="image name; the data dir is mounted at /data")
    ap.add_argument("--rows", type=int, default=ROWS)
    ap.add_argument("--queries", type=int, default=QUERIES)
    ap.add_argument("--check", type=int, default=40, help="how many queries to verify exactly")
    ap.add_argument("--bin", default=os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb"))
    args = ap.parse_args()

    created, batches, vectors, probes = gen(args.rows, args.queries, DIMS)

    own_dir = None
    if args.docker:
        own_dir = tempfile.mkdtemp(prefix="mvdb_scale_")
        data_dir = own_dir
        argv = ["docker", "run", "--rm", "-i", "-v", "%s:/data" % data_dir, args.docker,
                "--data-dir", "/data"]
    else:
        data_dir = args.data_dir or tempfile.mkdtemp(prefix="mvdb_scale_")
        own_dir = None if args.data_dir else data_dir
        os.makedirs(data_dir, exist_ok=True)
        argv = [args.bin, "--data-dir", data_dir]

    driver = Driver(argv)
    try:
        driver.send(created)
        driver.flush()
        driver.read_ack()

        start = time.time()
        for batch in batches:
            driver.send("BEGIN;")
            driver.send("INSERT INTO docs VALUES " + ",".join(batch) + ";")
            driver.send("COMMIT;")
            driver.flush()
            driver.read_ack()
            driver.read_ack()
            driver.read_ack()
        load_seconds = time.time() - start

        start = time.time()
        answers = []
        for sql, _, _ in probes:
            driver.send(sql)
            driver.flush()
            header, rows = driver.read_block()
            answers.append(rows)
        query_seconds = time.time() - start

        size = 0
        if args.data_dir or not args.docker:
            for root, _, files in os.walk(data_dir):
                for name in files:
                    try:
                        size += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass

        wrong = 0
        worst_recall = 1.0
        checked = 0
        for index in range(min(args.check, len(probes))):
            sql, vec, category = probes[index]
            want = exact_topk(vectors, vec, K, category if category is not None else None)
            got = []
            for row in answers[index]:
                got.append((int(row[0]), float(row[1])))
            overlap = len(set(pk for pk, _ in want) & set(pk for pk, _ in got))
            recall = overlap / float(len(want)) if want else 1.0
            worst_recall = min(worst_recall, recall)
            checked += 1
            ids_ok = [pk for pk, _ in want] == [pk for pk, _ in got]
            dists_ok = all(abs(w - g) <= 1e-4 * max(1.0, abs(w))
                           for (_, w), (_, g) in zip(want, got))
            if not (ids_ok and dists_ok):
                wrong += 1
                if wrong <= 3:
                    print("MISMATCH %s" % sql[:110])
                    print("  want %r" % want)
                    print("  got  %r" % got)

        print("rows=%d queries=%d dims=%d" % (args.rows, args.queries, DIMS))
        print("load:  %.1f ms  (%.1f us/row)" % (load_seconds * 1000.0,
                                                 load_seconds * 1e6 / max(1, args.rows)))
        print("query: %.1f us/query" % (query_seconds * 1e6 / max(1, len(probes))))
        print("data dir: %.2f MiB" % (size / (1024.0 * 1024.0)) if size else "data dir: (in container)")
        print("verified %d queries: %d wrong, worst Recall@%d = %.3f" % (
            checked, wrong, K, worst_recall))
        return 1 if wrong else 0
    finally:
        driver.close()
        if own_dir:
            shutil.rmtree(own_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
