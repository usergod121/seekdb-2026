#!/usr/bin/env python3
"""Scale and recovery against a model: 100000 rows, deletes, reinserts, restarts and a hard kill.

The formal tests include data sets larger than the memory budget and a restart in the middle of a
sequence, so this probe keeps an exact model of a 100000-row table (mixed types, a vector column and a
vector index) and checks the engine after every phase: after the bulk load, after a clean restart,
after deleting a slice and reinserting other rows, after a hard kill, and with a tiny cache budget so
the paged path does the work.
"""
import json
import os
import random
import shutil
import signal
import struct
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
PROBLEMS = []
CHECKS = [0]
ROWS = 100000
DIMS = 16


def f32(x):
    return struct.unpack("f", struct.pack("f", float(x)))[0]


class Engine:
    def __init__(self, data, env=None):
        self.data = data
        self.proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                                     env=env)
        self.buf = b""
        self.seq = 0
        if self.line() is None:
            raise RuntimeError("no handshake")

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 20)
            if not chunk:
                return None
            self.buf += chunk
        out, _, self.buf = self.buf.partition(b"\n")
        return out.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql, mode="exact"):
        if self.proc.poll() is not None:
            return None
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": mode}) + "\n").encode())
        raw = self.line()
        if raw is None:
            return None
        return json.loads(raw)

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()

    def close(self):
        if self.proc.poll() is None:
            try:
                self.seq += 1
                os.write(self.proc.stdin.fileno(),
                         (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
                self.line()
                self.proc.wait(timeout=300)
            except Exception:
                self.proc.kill()
        return self.proc.returncode


def check(label, condition, detail=""):
    CHECKS[0] += 1
    if not condition:
        PROBLEMS.append(label + ("  " + detail if detail else ""))
        print("FAIL  " + label + ("  " + detail if detail else ""))


def topk(model, query, k, category=None):
    scored = []
    for pk, (cat, vec) in model.items():
        if category is not None and cat != category:
            continue
        total = 0.0
        for a, b in zip(vec, query):
            d = a - f32(b)
            total += d * d
        scored.append((total, pk))
    scored.sort()
    return [pk for _, pk in scored[:k]]


def verify(engine, model, label):
    reply = engine.req("SELECT id FROM t ORDER BY id")
    if reply is None or reply.get("ok") is not True:
        check(label + " :: table readable", False, "process gone")
        return False
    got = [row[0] for row in reply["rows"]]
    check(label + " :: row set", got == sorted(model), "%d rows vs %d expected"
          % (len(got), len(model)))
    rng = random.Random(9)
    wrong = 0
    for _ in range(4):
        query = [round(rng.uniform(-1, 1), 4) for _ in range(DIMS)]
        literal = ",".join("%.4f" % v for v in query)
        want = topk(model, query, 10)
        for mode in ("exact", "ann"):
            reply = engine.req("SELECT id FROM t ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT 10"
                               % literal, mode)
            if reply is None or reply.get("ok") is not True:
                check(label + " :: topk %s" % mode, False, "process gone")
                return False
            ids = [row[0] for row in reply["rows"]]
            if ids != want:
                wrong += 1
        category = rng.randrange(4)
        want = topk(model, query, 10, "c%d" % category)
        reply = engine.req("SELECT id FROM t WHERE grp='c%d' "
                           "ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT 10" % (category, literal))
        if reply is None or reply.get("ok") is not True:
            check(label + " :: filtered topk", False, "process gone")
            return False
        if [row[0] for row in reply["rows"]] != want:
            wrong += 1
    check(label + " :: TopK matches the model", wrong == 0, "%d of 9 wrong" % wrong)
    return True


def main():
    rng = random.Random(20261112)
    tmp = tempfile.mkdtemp(prefix="mvdb_scale_model_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    model = {}
    try:
        engine = Engine(data)
        reply = engine.req("CREATE TABLE t (id BIGINT PRIMARY KEY, grp VARCHAR(8), note VARCHAR(16), "
                           "emb VECTOR(%d))" % DIMS)
        check("create table", reply is not None and reply.get("ok") is True, json.dumps(reply)[:120])

        batch = 2500
        for start in range(1, ROWS + 1, batch):
            parts = []
            for pk in range(start, min(start + batch, ROWS + 1)):
                vec = [round(rng.uniform(-1, 1), 4) for _ in range(DIMS)]
                cat = "c%d" % (pk % 4)
                model[pk] = (cat, [f32(v) for v in vec])
                parts.append("(%d,'%s','n%d','[%s]')" % (
                    pk, cat, pk, ",".join("%.4f" % v for v in vec)))
            reply = engine.req("INSERT INTO t VALUES " + ",".join(parts))
            if reply is None or reply.get("ok") is not True:
                check("bulk load at %d" % start, False, "process gone")
                return 1
        check("loaded %d rows" % ROWS, len(model) == ROWS)
        if not verify(engine, model, "after the bulk load"):
            return 1

        reply = engine.req("CREATE VECTOR INDEX idx ON t(emb)")
        check("create vector index", reply is not None and reply.get("ok") is True,
              json.dumps(reply)[:120])

        # clean restart
        check("clean shutdown", engine.close() == 0)
        engine = Engine(data)
        if not verify(engine, model, "after a clean restart"):
            return 1

        # delete a slice, reinsert different rows
        reply = engine.req("DELETE FROM t WHERE id > 1000 AND id <= 30000")
        check("delete slice affected", reply is not None and reply.get("ok") is True and
              reply.get("affected_rows") == 29000, json.dumps(reply)[:140] if reply else "gone")
        for pk in range(1001, 30001):
            model.pop(pk, None)
        parts = []
        for pk in range(ROWS + 1, ROWS + 2001):
            vec = [round(rng.uniform(-1, 1), 4) for _ in range(DIMS)]
            cat = "c%d" % (pk % 4)
            model[pk] = (cat, [f32(v) for v in vec])
            parts.append("(%d,'%s','n%d','[%s]')" % (
                pk, cat, pk, ",".join("%.4f" % v for v in vec)))
        reply = engine.req("INSERT INTO t VALUES " + ",".join(parts))
        check("reinsert 2000 rows", reply is not None and reply.get("ok") is True,
              json.dumps(reply)[:140] if reply else "gone")
        if not verify(engine, model, "after deletes and reinserts"):
            return 1

        # hard kill with an open transaction
        engine.req("BEGIN")
        engine.req("INSERT INTO t VALUES (%d,'c0','open','[%s]')" % (
            ROWS + 9999, ",".join("0.1000" for _ in range(DIMS))))
        engine.req("DELETE FROM t WHERE id <= 500")
        engine.kill()
        engine = Engine(data)
        if not verify(engine, model, "after a hard kill"):
            return 1

        # paged path: a cache budget far below the data size
        check("shutdown before the paged pass", engine.close() == 0)
        env = dict(os.environ, MVDB_CACHE_BUDGET_BYTES="1048576")
        engine = Engine(data, env=env)
        if not verify(engine, model, "with a 1 MiB cache budget"):
            return 1
        check("shutdown", engine.close() == 0)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
    print()
    print("checks: %d" % CHECKS[0])
    if PROBLEMS:
        print("problems: %d" % len(PROBLEMS))
        for item in PROBLEMS[:25]:
            print("  - " + item)
    else:
        print("no problems")
    sys.exit(1 if PROBLEMS else 0)
