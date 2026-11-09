#!/usr/bin/env python3
"""Checks the formal requirements the public package lists but does not ship a case for.

  * NaN, Infinity, wrong dimensions and float32 overflow are rejected, and the table is unchanged;
  * a failed statement never leaves partial data behind;
  * a committed transaction survives a hard kill, an uncommitted one never partially applies;
  * killing the process again while it is recovering still recovers the whole committed state;
  * data that does not fit the memory budget is still queried correctly (paged path);
  * TopK over that paged data filters first and breaks ties on the primary key.
"""
import os
import random
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
problems = []


def f32(x):
    return struct.unpack("f", struct.pack("f", x))[0]


class Session:
    def __init__(self, data_dir, env=None):
        self.proc = subprocess.Popen([BIN, "--data-dir", data_dir], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                                     env=env)
        self.buf = b""
        self.line()

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("engine closed its output")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def run(self, sql):
        os.write(self.proc.stdin.fileno(), (sql + "\n").encode("utf-8"))
        out = {"kind": None, "columns": None, "rows": [], "affected": 0, "code": ""}
        while True:
            line = self.line()
            if line == "READY":
                continue
            if line.startswith("OK"):
                out["kind"] = "ok"
                parts = line.split(" ", 1)
                out["affected"] = int(parts[1]) if len(parts) > 1 and parts[1] else 0
                return out
            if line.startswith("ERROR"):
                out["kind"] = "error"
                out["code"] = line.split(" ")[1] if len(line.split(" ")) > 1 else ""
                return out
            if line == "END":
                out["kind"] = "rows"
                return out
            if out["columns"] is None:
                out["columns"] = [] if line == "" else line.split("\t")
            else:
                out["rows"].append([] if line == "" else line.split("\t"))

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def expect(label, condition, detail=""):
    if condition:
        print("ok    " + label)
    else:
        print("FAIL  " + label + (" | " + detail if detail else ""))
        problems.append(label)


def temp_dir():
    tmp = tempfile.mkdtemp(prefix="mvdb_formal_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    return tmp, data


def vector_validation():
    tmp, data = temp_dir()
    s = Session(data)
    try:
        s.run("CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(2));")
        bad = {
            "NaN": "'[nan,0]'",
            "Infinity": "'[Infinity,0]'",
            "-Infinity": "'[-inf,0]'",
            "float32 overflow": "'[1e40,0]'",
            "just past the float32 range": "'[3.5e38,0]'",
            "wrong dimension (3 into 2)": "'[1,2,3]'",
            "wrong dimension (1 into 2)": "'[1]'",
            "not a vector": "'hello'",
        }
        for label, literal in bad.items():
            result = s.run("INSERT INTO t VALUES (1, %s);" % literal)
            expect("%s is rejected" % label, result["kind"] == "error", str(result))
        rows = s.run("SELECT id FROM t;")["rows"]
        expect("no rejected insert left a row behind", rows == [], str(rows))
        good = s.run("INSERT INTO t VALUES (1, '[0.5,1e-40]');")
        expect("subnormal but representable values are accepted", good["kind"] == "ok", str(good))
    finally:
        s.close()
        shutil.rmtree(tmp, ignore_errors=True)


def statement_atomicity():
    tmp, data = temp_dir()
    s = Session(data)
    try:
        s.run("CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(2));")
        s.run("INSERT INTO t VALUES (1,'[0,0]');")
        bad = s.run("INSERT INTO t VALUES (2,'[1,1]'),(3,'[1]');")
        expect("a multi-row insert with a bad row fails", bad["kind"] == "error", str(bad))
        rows = s.run("SELECT id FROM t;")["rows"]
        expect("the good row of the failed insert is not stored", rows == [["1"]], str(rows))
        bad2 = s.run("INSERT INTO t VALUES (2,'[1,1]'),(1,'[2,2]');")
        expect("a duplicate inside a multi-row insert fails", bad2["kind"] == "error", str(bad2))
        rows = s.run("SELECT id FROM t;")["rows"]
        expect("the first row of that insert is not stored either", rows == [["1"]], str(rows))
    finally:
        s.close()
        shutil.rmtree(tmp, ignore_errors=True)


def crash_consistency():
    tmp, data = temp_dir()
    s = Session(data)
    try:
        s.run("CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(2));")
        s.run("INSERT INTO t VALUES (1,'[0,0]');")
        s.run("BEGIN;")
        s.run("INSERT INTO t VALUES (2,'[1,1]');")
        s.kill()
        s = Session(data)
        rows = s.run("SELECT id FROM t;")["rows"]
        expect("committed row survives the kill, open transaction does not",
               rows == [["1"]], str(rows))
        s.kill()
        s = Session(data)
        rows = s.run("SELECT id FROM t;")["rows"]
        expect("a second hard kill right after recovery keeps the committed state",
               rows == [["1"]], str(rows))
    finally:
        s.close()
        shutil.rmtree(tmp, ignore_errors=True)


def crash_during_recovery():
    """A long log makes the recovery window wide enough to kill the process inside it."""
    tmp, data = temp_dir()
    s = Session(data)
    try:
        s.run("CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(16));")
        rows_per = 200
        batches = 25
        for batch in range(batches):
            values = ",".join(
                "(%d,'[%s]')" % (batch * rows_per + i + 1,
                                 ",".join("%.4f" % ((i * j) % 11 / 11.0) for j in range(16)))
                for i in range(rows_per))
            s.run("BEGIN;")
            s.run("INSERT INTO t VALUES " + values + ";")
            s.run("COMMIT;")
        committed = batches * rows_per
        s.kill()

        # kill the process a few times while it is replaying the log
        for attempt in range(3):
            proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.02 + 0.02 * attempt)
            proc.send_signal(signal.SIGKILL)
            proc.wait()

        s = Session(data)
        rows = s.run("SELECT id FROM t;")["rows"]
        expect("every committed row is there after repeated kills during recovery",
               len(rows) == committed, "%d rows, expected %d" % (len(rows), committed))
        ids = sorted(int(r[0]) for r in rows)
        expect("no duplicate and no missing primary key", ids == list(range(1, committed + 1)),
               "first=%r last=%r" % (ids[:1], ids[-1:]))
    finally:
        s.close()
        shutil.rmtree(tmp, ignore_errors=True)


def over_budget_data():
    """A budget of 1 MiB forces the paged path for a data set of a few MiB."""
    tmp, data = temp_dir()
    env = dict(os.environ, MVDB_CACHE_BUDGET_BYTES="1048576")
    rng = random.Random(31337)
    dims = 32
    rows = 4000
    vectors = {}
    s = Session(data, env=env)
    try:
        s.run("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), emb VECTOR(%d));" % dims)
        batch = 200
        for start in range(0, rows, batch):
            parts = []
            for i in range(start, min(start + batch, rows)):
                vec = [round(rng.uniform(-1, 1), 4) for _ in range(dims)]
                vectors[i + 1] = vec
                parts.append("(%d,'c%d','[%s]')" % (
                    i + 1, i % 4, ",".join("%.4f" % c for c in vec)))
            s.run("BEGIN;")
            s.run("INSERT INTO docs VALUES " + ",".join(parts) + ";")
            s.run("COMMIT;")

        count = s.run("SELECT id FROM docs;")["rows"]
        expect("every row of the over-budget table is readable", len(count) == rows,
               "%d rows" % len(count))

        def exact_topk(qvec, k, category=None):
            scored = []
            for pk, vec in vectors.items():
                if category is not None and (pk - 1) % 4 != category:
                    continue
                total = 0.0
                for a, b in zip(vec, qvec):
                    d = a - b
                    total += d * d
                scored.append((total, pk))
            scored.sort()
            return [(pk, total ** 0.5) for _, pk in scored[:k]]

        wrong = 0
        for probe in range(4):
            qvec = [round(rng.uniform(-1, 1), 4) for _ in range(dims)]
            literal = ",".join("%.4f" % c for c in qvec)
            unfiltered = s.run(
                "SELECT id, L2_DISTANCE(emb,'[%s]') AS d FROM docs ORDER BY d LIMIT 10;" % literal)
            want = exact_topk(qvec, 10)
            got = [(int(r[0]), float(r[1])) for r in unfiltered["rows"]]
            if [pk for pk, _ in want] != [pk for pk, _ in got]:
                wrong += 1
            category = probe % 4
            filtered = s.run(
                "SELECT id, L2_DISTANCE(emb,'[%s]') AS d FROM docs WHERE category = 'c%d' "
                "ORDER BY d LIMIT 10;" % (literal, category))
            want_f = exact_topk(qvec, 10, category)
            got_f = [(int(r[0]), float(r[1])) for r in filtered["rows"]]
            if [pk for pk, _ in want_f] != [pk for pk, _ in got_f]:
                wrong += 1
        expect("TopK over the paged data matches the exact answer (filtered and unfiltered)",
               wrong == 0, "%d wrong of 8" % wrong)

        s.close()
        s = Session(data, env=env)
        after = s.run("SELECT id FROM docs;")["rows"]
        expect("the over-budget table survives a restart", len(after) == rows,
               "%d rows" % len(after))
    finally:
        s.close()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    vector_validation()
    statement_atomicity()
    crash_consistency()
    crash_during_recovery()
    over_budget_data()
    print()
    print("%d formal requirement check(s) failed" % len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
