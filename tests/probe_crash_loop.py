#!/usr/bin/env python3
"""Randomised crash loop.

Spec 2.3: "已确认提交的数据不丢，未提交事务不能部分生效". A single crash test can pass by luck, so this
probe kills the engine at a random point inside a transaction, repeatedly, and after every restart
checks that the table holds exactly the rows that were acknowledged before the kill - no lost
commit, no half-applied transaction, no duplicate or missing primary key.
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
import time

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
DIMS = 8
ITERATIONS = int(os.environ.get("MVDB_CRASH_ITERATIONS", "14"))
PROBLEMS = []


def f32(x):
    return struct.unpack("f", struct.pack("f", x))[0]


class S:
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
                raise RuntimeError("engine closed its output")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql):
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": "exact"}) + "\n").encode())
        return json.loads(self.line())

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()

    def close(self):
        try:
            self.seq += 1
            os.write(self.proc.stdin.fileno(),
                     (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
            self.line()
            self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()


def check(label, condition, detail=""):
    if not condition:
        PROBLEMS.append(label + ("  " + detail if detail else ""))
        print("FAIL  " + label + ("  " + detail if detail else ""))


def read_all(session):
    """Returns {pk: (category, tags, vector)} as stored, or None if the query failed."""
    reply = session.req("SELECT id, category, tags, emb FROM crash ORDER BY id")
    if not reply.get("ok"):
        return None
    out = {}
    for row in reply.get("rows") or []:
        pk, cat, tags, vec = row
        out[pk] = (cat, tags, [f32(v) for v in vec])
    return out


def main():
    rng = random.Random(20261111)
    tmp = tempfile.mkdtemp(prefix="mvdb_crash_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    committed = {}   # acknowledged state: pk -> (category, tags, vector)
    next_pk = 1
    crashes = 0
    try:
        s = S(data)
        s.req("CREATE TABLE crash (id BIGINT PRIMARY KEY, category VARCHAR(8), tags VARCHAR(16), "
              "emb VECTOR(%d))" % DIMS)
        for iteration in range(ITERATIONS):
            # A transaction with several rows, committed and acknowledged.
            rows = []
            for _ in range(rng.randint(1, 12)):
                vec = [round(rng.uniform(-1, 1), 4) for _ in range(DIMS)]
                cat = "c%d" % (next_pk % 5)
                tags = "t%d" % (next_pk % 3)
                rows.append((next_pk, cat, tags, [f32(v) for v in vec],
                             "(%d,'%s','%s','[%s]')" % (next_pk, cat, tags,
                                                        ",".join("%.4f" % v for v in vec))))
                next_pk += 1
            s.req("BEGIN")
            reply = s.req("INSERT INTO crash VALUES " + ",".join(r[4] for r in rows))
            if reply.get("ok") is not True:
                check("insert accepted in iteration %d" % iteration, False, json.dumps(reply)[:160])
                break
            reply = s.req("COMMIT")
            if reply.get("ok") is not True:
                check("commit acknowledged in iteration %d" % iteration, False, json.dumps(reply)[:160])
                break
            for pk, cat, tags, vec, _ in rows:
                committed[pk] = (cat, tags, vec)

            # Then start a transaction that must NOT survive, and kill the process inside it.
            victim = next_pk
            next_pk += 1
            s.req("BEGIN")
            s.req("INSERT INTO crash VALUES (%d,'k','z','[%s]')" % (
                victim, ",".join("0.5000" for _ in range(DIMS))))
            s.req("UPDATE crash SET category='half' WHERE id=1")
            if iteration % 3 == 2:
                s.req("DELETE FROM crash WHERE id=2")
            time.sleep(rng.uniform(0.0, 0.004))
            s.kill()
            crashes += 1

            s = S(data)
            state = read_all(s)
            if state is None:
                check("table readable after crash %d" % iteration, False)
                break
            if state != committed:
                missing = sorted(set(committed) - set(state))[:5]
                extra = sorted(set(state) - set(committed))[:5]
                changed = [pk for pk in set(state) & set(committed) if state[pk] != committed[pk]][:5]
                check("state after crash %d matches the acknowledged state" % iteration, False,
                      "missing=%r extra=%r changed=%r" % (missing, extra, changed))
                # resynchronise so one difference does not fail every later iteration
                committed = dict(state)

            # A crash in the middle of recovery must not lose anything either.
            if iteration % 4 == 3:
                proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(0.01)
                proc.send_signal(signal.SIGKILL)
                proc.wait()
                crashes += 1
                s.close()
                s = S(data)
                state = read_all(s)
                if state != committed:
                    check("state after a crash during recovery (%d)" % iteration, False,
                          "rows=%d expected=%d" % (len(state or {}), len(committed)))
                    committed = dict(state or {})

        # Vector search on the recovered table must agree with the recovered rows.
        state = read_all(s) or {}
        if state:
            qvec = [0.25, -0.25, 0.5, 0.0, 0.0, 0.1, -0.1, 0.0]
            reply = s.req("SELECT id, L2_DISTANCE(emb,'[%s]') AS d FROM crash ORDER BY d LIMIT 5"
                          % ",".join("%.4f" % v for v in qvec))
            got = [row[0] for row in (reply.get("rows") or [])]
            scored = []
            for pk, (_cat, _tags, vec) in state.items():
                total = 0.0
                for a, b in zip(vec, qvec):
                    d = a - f32(b)
                    total += d * d
                scored.append((total, pk))
            scored.sort()
            want = [pk for _, pk in scored[:5]]
            check("TopK after repeated crashes matches the recovered rows", got == want,
                  "want %r got %r" % (want, got))
        check("row count after %d crashes" % crashes, len(state) == len(committed),
              "%d vs %d" % (len(state), len(committed)))
        s.close()
        print()
        print("iterations: %d, crashes: %d, rows: %d" % (ITERATIONS, crashes, len(committed)))
        if PROBLEMS:
            print("problems: %d" % len(PROBLEMS))
            for p in PROBLEMS:
                print("  - " + p)
            return 1
        print("no problems")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
