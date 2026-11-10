#!/usr/bin/env python3
"""Splits the write phase into its parts: transaction overhead (COMMIT durability), statement parsing
and row insertion, so an optimisation can be aimed at the part that actually costs the time."""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
DIMS = 128


class E:
    def __init__(self, data):
        self.proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""
        self.seq = 0
        self.line()

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 16)
            if not chunk:
                raise RuntimeError("closed")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line

    def req(self, sql):
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": "exact"}) + "\n").encode())
        reply = json.loads(self.line())
        if reply.get("ok") is not True and not sql.startswith("DUP"):
            raise RuntimeError("%r -> %r" % (sql[:60], reply))
        return reply

    def close(self):
        try:
            self.seq += 1
            os.write(self.proc.stdin.fileno(),
                     (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
            self.line()
            self.proc.wait(timeout=60)
        except Exception:
            self.proc.kill()


def main():
    rng = random.Random(7)
    vectors = [[round(rng.uniform(-1, 1), 4) for _ in range(DIMS)] for _ in range(10000)]
    rows = ["(%d,'c%d','doc %d','[%s]')" % (i + 1, i % 8, i + 1,
                                            ",".join("%.4f" % v for v in vectors[i]))
            for i in range(10000)]
    tmp = tempfile.mkdtemp(prefix="mvdb_split_")
    out = {}
    try:
        data = os.path.join(tmp, "a")
        os.makedirs(data)
        e = E(data)
        e.req("CREATE TABLE t (id BIGINT PRIMARY KEY, category VARCHAR(8), title VARCHAR(32), "
              "emb VECTOR(%d))" % DIMS)
        t0 = time.perf_counter()
        for _ in range(100):
            e.req("BEGIN")
            e.req("COMMIT")
        out["100_empty_tx_s"] = round(time.perf_counter() - t0, 4)
        e.close()

        data = os.path.join(tmp, "b")
        os.makedirs(data)
        e = E(data)
        e.req("CREATE TABLE t (id BIGINT PRIMARY KEY, category VARCHAR(8), title VARCHAR(32), "
              "emb VECTOR(%d))" % DIMS)
        t0 = time.perf_counter()
        e.req("BEGIN")
        for r in rows:
            e.req("INSERT INTO t VALUES " + r)
        e.req("COMMIT")
        out["10000_single_row_statements_s"] = round(time.perf_counter() - t0, 4)
        e.close()

        data = os.path.join(tmp, "c")
        os.makedirs(data)
        e = E(data)
        e.req("CREATE TABLE t (id BIGINT PRIMARY KEY, category VARCHAR(8), title VARCHAR(32), "
              "emb VECTOR(%d))" % DIMS)
        t0 = time.perf_counter()
        for start in range(0, 10000, 100):
            e.req("BEGIN")
            e.req("INSERT INTO t VALUES " + ",".join(rows[start:start + 100]))
            e.req("COMMIT")
        out["10000_in_100_batches_s"] = round(time.perf_counter() - t0, 4)
        peak = 0
        with open("/proc/%d/status" % e.proc.pid) as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    peak = int(line.split()[1])
        e.close()
        out["peak_rss_kb"] = peak
        print(json.dumps(out))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
