# -*- coding: utf-8 -*-
"""Times the shutdown of a database holding a cached table.

The index-boundary probe left a process alive after the shutdown ack, so this measures the ack and the
actual exit separately, for a small table and for the cached 10k x 128 shape.
"""
import json
import os
import random
import subprocess
import sys
import tempfile
import time

BIN = sys.argv[1] if len(sys.argv) > 1 else "/mnt/e/mvdb/build/minivectordb"
ROWS = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
DIMS = 128


def fmt(vec):
    return ",".join("%.4f" % v for v in vec)


def run(rows):
    rng = random.Random(7)
    data = tempfile.mkdtemp(prefix="mvdb_sd_")
    proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    buf = b""
    seq = 0

    def line(timeout=120):
        nonlocal buf
        deadline = time.time() + timeout
        while b"\n" not in buf:
            if time.time() > deadline:
                return None
            os.set_blocking(proc.stdout.fileno(), False)
            try:
                chunk = os.read(proc.stdout.fileno(), 1 << 20)
            except BlockingIOError:
                time.sleep(0.001)
                continue
            finally:
                os.set_blocking(proc.stdout.fileno(), True)
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
    req("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), "
        "name VARCHAR(16), emb VECTOR(%d))" % DIMS)
    for start in range(0, rows, 500):
        values = ",".join("(%d,'c%d','u%05d','[%s]')"
                          % (i + 1, i % 8, i, fmt([round(rng.uniform(-1, 1), 4)
                                                   for _ in range(DIMS)]))
                          for i in range(start, min(start + 500, rows)))
        reply = req("INSERT INTO docs VALUES " + values)
        if reply.get("ok") is not True:
            print("insert failed: %r" % (reply,))
            return
    req("SELECT id FROM docs WHERE category='c1' ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT 10"
        % fmt([0.1] * DIMS))
    seq += 1
    started = time.time()
    os.write(proc.stdin.fileno(), (json.dumps({"id": seq, "op": "shutdown"}) + "\n").encode())
    ack = line(timeout=180)
    ack_at = time.time() - started
    try:
        code = proc.wait(timeout=180)
        exit_at = time.time() - started
        print("rows %6d  ack=%r after %.3f s  exit=%s after %.3f s" % (rows, (ack or "")[:60], ack_at, code, exit_at))
    except subprocess.TimeoutExpired:
        print("rows %6d  ack=%r after %.3f s  exit: STILL ALIVE after 180 s" % (rows, (ack or "")[:60], ack_at))
        proc.kill()


run(1000)
run(ROWS)
