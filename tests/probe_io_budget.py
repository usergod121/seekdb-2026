#!/usr/bin/env python3
"""Where do the measured bytes go, and how long does a restart take?

The graded metric weights disk I/O and peak memory at 20% each, and B counts physical reads and writes
on /data for the whole run. This probe loads the graded shape twice (with and without the vector
column), reads the kernel's per-process byte counters for the engine, and then restarts the engine on
the same directory to time recovery from the write-ahead log.

Usage: probe_io_budget.py [--binary PATH] [--rows N] [--dims N]
"""
import argparse
import json
import os
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

    def io_bytes(self):
        """Physical read/write bytes charged to the engine process so far."""
        try:
            with open("/proc/%d/io" % self.proc.pid, "r") as handle:
                fields = dict(line.split(": ") for line in handle.read().splitlines())
        except OSError:
            return None
        return (int(fields.get("read_bytes", 0)), int(fields.get("write_bytes", 0)))

    def shutdown(self):
        self.seq += 1
        os.write(self.proc.stdin.fileno(), (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
        self.line()
        self.proc.wait(timeout=120)


def load(binary, data, rows, dims, with_vector):
    import random
    rng = random.Random(4242)
    s = Session(binary, data)
    if with_vector:
        s.req("CREATE TABLE docs (id BIGINT PRIMARY KEY, tag VARCHAR(8), emb VECTOR(%d))" % dims)
    else:
        s.req("CREATE TABLE docs (id BIGINT PRIMARY KEY, tag VARCHAR(8))")
    before = s.io_bytes()
    for start in range(0, rows, 100):
        if with_vector:
            values = ",".join("(%d,'t%d','[%s]')" % (i + 1, i % 8,
                                                     fmt([round(rng.uniform(-1, 1), 4) for _ in range(dims)]))
                              for i in range(start, min(start + 100, rows)))
        else:
            values = ",".join("(%d,'t%d')" % (i + 1, i % 8)
                              for i in range(start, min(start + 100, rows)))
        reply = s.req("INSERT INTO docs VALUES " + values)
        if reply.get("ok") is not True:
            raise SystemExit("insert failed: %r" % (reply,))
    after = s.io_bytes()
    s.req("SELECT id FROM docs WHERE tag='t1' ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT 10"
          % fmt([0.0] * dims) if with_vector else "SELECT id FROM docs LIMIT 10")
    s.shutdown()
    data_bytes = sum(os.path.getsize(os.path.join(data, name)) for name in os.listdir(data))
    return before, after, data_bytes


def restart(binary, data):
    started = time.time()
    s = Session(binary, data)
    ready_at = time.time() - started
    started = time.time()
    reply = s.req("SELECT id FROM docs ORDER BY id LIMIT 3")
    query_at = time.time() - started
    s.shutdown()
    return ready_at, query_at, reply.get("ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default="/mnt/e/mvdb/build/minivectordb")
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--dims", type=int, default=128)
    args = ap.parse_args()

    for with_vector in (True, False):
        data = tempfile.mkdtemp(prefix="mvdb_io_")
        before, after, data_bytes = load(args.binary, data, args.rows, args.dims, with_vector)
        ready_at, query_at, ok = restart(args.binary, data)
        label = "128-dim vector" if with_vector else "no vector column"
        if before and after:
            print("%-16s rows %d  write_bytes %d  read_bytes %d  files on /data %d bytes  "
                  "restart ready %.3f s  first query %.3f s  (%s)"
                  % (label, args.rows, after[1] - before[1], after[0] - before[0], data_bytes,
                     ready_at, query_at, "ok" if ok else "FAILED"))
        else:
            print("%-16s rows %d  /proc io unavailable  files on /data %d bytes  restart ready %.3f s"
                  % (label, args.rows, data_bytes, ready_at))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
