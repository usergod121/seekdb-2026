#!/usr/bin/env python3
"""Where do the measured bytes go, and what does a restart cost?

Loads the graded shape twice (with and without the vector column), then restarts the engine on the same
directory to time recovery. The engine reports its own /proc/self/io counters at shutdown (it prints
them when MVDB_PROFILE is set), so the numbers cover the whole run including the close, which a probe
reading /proc while the process is alive would miss.
"""
import argparse
import json
import os
import re
import subprocess
import tempfile
import time

PROFILE_IO = re.compile(r"\[profile\] io (read_bytes|write_bytes):\s*(\d+)")
PROFILE_CLOSE = re.compile(r"\[profile\] close: wal_bytes=(\d+) dirty_page_bytes=(\d+) keep_log=(\d)")


def fmt(vec):
    return ",".join("%.4f" % v for v in vec)


class Session:
    def __init__(self, binary, data):
        env = dict(os.environ)
        env["MVDB_PROFILE"] = "1"
        self.proc = subprocess.Popen([binary, "--data-dir", data], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, env=env)
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
        err = self.proc.stderr.read().decode("utf-8", "replace")
        self.proc.wait(timeout=180)
        return err


def load(binary, data, rows, dims, with_vector):
    import random
    rng = random.Random(4242)
    s = Session(binary, data)
    if with_vector:
        s.req("CREATE TABLE docs (id BIGINT PRIMARY KEY, tag VARCHAR(8), emb VECTOR(%d))" % dims)
    else:
        s.req("CREATE TABLE docs (id BIGINT PRIMARY KEY, tag VARCHAR(8))")
    started = time.perf_counter()
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
    load_ms = (time.perf_counter() - started) * 1000.0
    if with_vector:
        s.req("SELECT id FROM docs WHERE tag='t1' ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT 10"
              % fmt([0.0] * dims))
    else:
        s.req("SELECT id FROM docs LIMIT 10")
    err = s.shutdown()
    io_bytes = {m.group(1): int(m.group(2)) for m in PROFILE_IO.finditer(err)}
    close = PROFILE_CLOSE.search(err)
    data_bytes = sum(os.path.getsize(os.path.join(data, name)) for name in os.listdir(data))
    wal_bytes = int(close.group(1)) if close else 0
    page_bytes = int(close.group(2)) if close else 0
    keep_log = close.group(3) == "1" if close else False
    return load_ms, io_bytes, data_bytes, wal_bytes, page_bytes, keep_log


def restart(binary, data):
    started = time.perf_counter()
    s = Session(binary, data)
    ready_ms = (time.perf_counter() - started) * 1000.0
    started = time.perf_counter()
    reply = s.req("SELECT id FROM docs ORDER BY id LIMIT 3")
    query_ms = (time.perf_counter() - started) * 1000.0
    s.shutdown()
    return ready_ms, query_ms, reply.get("ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default="/mnt/e/mvdb/build/minivectordb")
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--dims", type=int, default=128)
    args = ap.parse_args()

    for with_vector in (True, False):
        data = tempfile.mkdtemp(prefix="mvdb_io_")
        load_ms, io_bytes, data_bytes, wal_bytes, page_bytes, keep_log = load(
            args.binary, data, args.rows, args.dims, with_vector)
        ready_ms, query_ms, ok = restart(args.binary, data)
        label = "128-dim vector" if with_vector else "no vector column"
        print("%-16s rows %d  own_writes %d  own_reads %d  final_files %d  log %d  dirty_pages %d  "
              "keep_log %d  load %.0f ms  restart_ready %.0f ms  first_query %.0f ms (%s)"
              % (label, args.rows, io_bytes.get("write_bytes", -1), io_bytes.get("read_bytes", -1),
                 data_bytes, wal_bytes, page_bytes, 1 if keep_log else 0, load_ms, ready_ms, query_ms,
                 "ok" if ok else "FAILED"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
