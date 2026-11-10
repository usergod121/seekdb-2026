#!/usr/bin/env python3
"""Measures the three quantities the performance score is built from (spec 3.3):

  T_write, T_query - wall time of the bulk write phase and of the TopK query phase
  B               - bytes written by the engine process (its own /proc/<pid>/io, not the whole disk)
  M               - peak resident memory (VmHWM)

Runs 10000 rows of 128 dimensions and 1000 TopK queries by default, which is the shape described in
the task (bulk write, then vector TopK), and prints one JSON line so runs can be compared.
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
ROWS = int(os.environ.get("MVDB_ROWS", "10000"))
DIMS = int(os.environ.get("MVDB_DIMS", "128"))
QUERIES = int(os.environ.get("MVDB_QUERIES", "1000"))
BATCH = int(os.environ.get("MVDB_BATCH", "100"))
TOPK = int(os.environ.get("MVDB_TOPK", "10"))


class Engine:
    def __init__(self, data):
        env = dict(os.environ)
        self.proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                                     env=env)
        self.buf = b""
        self.seq = 0
        self.line()

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 16)
            if not chunk:
                raise RuntimeError("engine closed its output")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line

    def req(self, sql):
        self.seq += 1
        body = {"id": self.seq, "op": "execute", "sql": sql, "search_mode": "exact"}
        os.write(self.proc.stdin.fileno(), (json.dumps(body) + "\n").encode())
        reply = json.loads(self.line())
        if reply.get("ok") is not True:
            raise RuntimeError("statement failed: %r -> %r" % (sql[:80], reply))
        return reply

    def io_counters(self):
        with open("/proc/%d/io" % self.proc.pid) as fh:
            out = {}
            for line in fh:
                key, _, value = line.partition(":")
                try:
                    out[key.strip()] = int(value.strip())
                except ValueError:
                    pass
            return out

    def peak_rss_kb(self):
        with open("/proc/%d/status" % self.proc.pid) as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
        return 0

    def close(self):
        try:
            self.seq += 1
            os.write(self.proc.stdin.fileno(),
                     (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
            self.line()
            self.proc.wait(timeout=60)
        except Exception:
            self.proc.kill()


def fmt(vec):
    return ",".join("%.4f" % v for v in vec)


def main():
    rng = random.Random(4242)
    vectors = [[round(rng.uniform(-1, 1), 4) for _ in range(DIMS)] for _ in range(ROWS)]
    probes = [[round(rng.uniform(-1, 1), 4) for _ in range(DIMS)] for _ in range(QUERIES)]
    tmp = tempfile.mkdtemp(prefix="mvdb_bench_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    result = {"rows": ROWS, "dims": DIMS, "queries": QUERIES, "batch": BATCH, "topk": TOPK}
    try:
        eng = Engine(data)
        eng.req("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), "
                "title VARCHAR(32), emb VECTOR(%d))" % DIMS)
        base = eng.io_counters()
        t0 = time.perf_counter()
        for start in range(0, ROWS, BATCH):
            parts = ["(%d,'c%d','doc %d','[%s]')" % (
                i + 1, (i % 8), i + 1, fmt(vectors[i])) for i in range(start, min(start + BATCH, ROWS))]
            eng.req("BEGIN")
            eng.req("INSERT INTO docs VALUES " + ",".join(parts))
            eng.req("COMMIT")
        t_write = time.perf_counter() - t0
        mid = eng.io_counters()
        lat = []
        lat_filtered = []
        t0 = time.perf_counter()
        for index, q in enumerate(probes):
            # spec 3.3: half of the queries carry an equality filter on an ordinary column
            filtered = (index % 2 == 1)
            sql = ("SELECT id FROM docs WHERE category='c%d' "
                   "ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT %d" % (index % 8, fmt(q), TOPK)
                   if filtered else
                   "SELECT id FROM docs ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT %d" % (fmt(q), TOPK))
            begin = time.perf_counter()
            reply = eng.req(sql)
            elapsed = (time.perf_counter() - begin) * 1e6
            (lat_filtered if filtered else lat).append(elapsed)
            rows = reply.get("rows") or []
            if len(rows) != min(TOPK, ROWS // 8 if filtered else ROWS):
                raise RuntimeError("unexpected row count %d for %r" % (len(rows), sql[:60]))
        t_query = time.perf_counter() - t0
        peak = eng.peak_rss_kb()
        end = eng.io_counters()
        eng.close()
        result.update({
            "t_write_s": round(t_write, 4),
            "t_query_total_s": round(t_query, 4),
            "query_median_us": round(statistics.median(lat), 2),
            "query_p90_us": round(sorted(lat)[int(len(lat) * 0.9)], 2),
            "query_filtered_median_us": round(statistics.median(lat_filtered), 2),
            "write_bytes": mid.get("write_bytes", 0) - base.get("write_bytes", 0),
            "write_bytes_total": end.get("write_bytes", 0) - base.get("write_bytes", 0),
            "peak_rss_kb": peak,
        })
        data_bytes = sum(os.path.getsize(os.path.join(data, f)) for f in os.listdir(data))
        result["data_dir_bytes"] = data_bytes
        print(json.dumps(result))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
