#!/usr/bin/env python3
"""Where does the write phase go?

The benchmark's write phase loads 10000 rows of 128 dimensions as 100 batches, each in a transaction.
On fast storage the device flush stops being the bottleneck, so this probe measures the same load with
three payload shapes to separate the cost of the vector literal (parsing float text), the row payload
copy and the transaction machinery:

  A. a vector column of 128 dimensions (the benchmark shape)
  B. a vector column of 1 dimension (same machinery, almost no parsing)
  C. no vector column at all (no vector parsing, smaller rows)

All three run the same number of rows and commits, so the differences isolate those costs.
"""
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
ROWS = int(os.environ.get("ROWS", "10000"))
BATCH = int(os.environ.get("BATCH", "100"))


class Engine:
    def __init__(self, data):
        self.proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""
        self.seq = 0
        self.line()

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 20)
            if not chunk:
                raise RuntimeError("engine closed its output")
            self.buf += chunk
        out, _, self.buf = self.buf.partition(b"\n")
        return out.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql):
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql}) + "\n").encode())
        reply = json.loads(self.line())
        if reply.get("ok") is not True:
            raise RuntimeError(json.dumps(reply)[:200])
        return reply

    def close(self):
        try:
            self.seq += 1
            os.write(self.proc.stdin.fileno(),
                     (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
            self.line()
            self.proc.wait(timeout=120)
        except Exception:
            self.proc.kill()


def run(label, dims, rng):
    data = tempfile.mkdtemp(prefix="mvdb_writecost_")
    if dims:
        ddl = "CREATE TABLE t (id BIGINT PRIMARY KEY, note VARCHAR(16), emb VECTOR(%d))" % dims
    else:
        ddl = "CREATE TABLE t (id BIGINT PRIMARY KEY, note VARCHAR(16), extra VARCHAR(32))"
    vectors = [[round(rng.uniform(-1, 1), 4) for _ in range(dims or 1)] for _ in range(ROWS)]
    engine = Engine(data)
    engine.req(ddl)
    batch_time = []
    for start in range(0, ROWS, BATCH):
        if dims:
            body = ",".join("(%d,'n%d','[%s]')" % (
                i + 1, i, ",".join("%.4f" % v for v in vectors[i]))
                for i in range(start, min(start + BATCH, ROWS)))
        else:
            body = ",".join("(%d,'n%d','x%d')" % (i + 1, i, i)
                            for i in range(start, min(start + BATCH, ROWS)))
        begin = time.perf_counter()
        engine.req("BEGIN")
        engine.req("INSERT INTO t VALUES " + body)
        engine.req("COMMIT")
        batch_time.append((time.perf_counter() - begin) * 1000.0)
    total = sum(batch_time)
    engine.close()
    shutil.rmtree(data, ignore_errors=True)
    print("%-28s total %8.1f ms   per batch median %6.2f ms   per row %6.2f us"
          % (label, total, statistics.median(batch_time), total * 1000.0 / ROWS))
    return total


def main():
    rng = random.Random(20261113)
    print("rows=%d batch=%d" % (ROWS, BATCH))
    for round_index in (1, 2):
        print("--- round %d" % round_index)
        run("A 128-dim vector", 128, rng)
        run("B 1-dim vector", 1, rng)
        run("C no vector column", 0, rng)
    return 0


if __name__ == "__main__":
    sys.exit(main())
