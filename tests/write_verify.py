#!/usr/bin/env python3
"""Verifies the write side the way the performance project is scored.

The performance project is only valid if the rows are correct, complete and still there after a restart,
and if a hard kill keeps exactly the acknowledged transactions. This streams the graded shape (100
batches of 100 rows, each in an explicit transaction), kills the process in the middle, and then checks
what survived: count, primary keys, and column contents.
"""
import argparse
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
DIMS = 16          # a narrower vector keeps the check fast; the storage path is identical
BATCH = 100
BATCHES = 100


class Driver:
    def __init__(self, data_dir):
        self.proc = subprocess.Popen([BIN, "--data-dir", data_dir], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
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

    def send(self, sql):
        os.write(self.proc.stdin.fileno(), (sql + "\n").encode("utf-8"))

    def ack(self):
        while True:
            line = self.line()
            if line.startswith("OK"):
                return
            if line.startswith("ERROR"):
                raise RuntimeError("statement failed: " + line)

    def block(self):
        while True:
            line = self.line()
            if line.startswith("ERROR"):
                raise RuntimeError("query failed: " + line)
            if line.startswith("OK") or line == "READY":
                continue
            rows = []
            while True:
                inner = self.line()
                if inner == "END":
                    return rows
                rows.append(inner.split("\t"))

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


def make_rows(batches):
    rng = random.Random(4242)
    out = []
    pk = 1
    for _ in range(batches):
        rows = []
        for _ in range(BATCH):
            vec = [round(rng.uniform(-1.0, 1.0), 4) for _ in range(DIMS)]
            rows.append((pk, pk % 8, "doc %d" % pk, vec))
            pk += 1
        out.append(rows)
    return out


def insert_sql(rows):
    parts = []
    for pk, category, title, vec in rows:
        text = ",".join("%.4f" % c for c in vec)
        parts.append("(%d,'c%d','%s','[%s]')" % (pk, category, title, text))
    return "INSERT INTO docs VALUES " + ",".join(parts) + ";"


def verify(driver, expect_rows, where):
    """Checks count, keys and contents of the whole table."""
    driver.send("SELECT * FROM docs;")
    got = driver.block()
    got_pk = {}
    for row in got:
        got_pk[int(row[0])] = (row[1], row[2], row[3])
    problems = []
    if len(got) != len(expect_rows):
        problems.append("%s: %d rows, expected %d (max pk seen %s)" % (
            where, len(got), len(expect_rows),
            str(max((int(r[0]) for r in got), default=None))))
    for pk, category, title, vec in expect_rows:
        if pk not in got_pk:
            problems.append("%s: row %d is missing" % (where, pk))
            break
        have = got_pk[pk]
        if int(have[0][1:]) != category or have[1] != title:
            problems.append("%s: row %d has %r, expected category c%d title %r" % (
                where, pk, have[:2], category, title))
            break
        values = [float(c) for c in have[2].strip("[]").split(",")]
        if len(values) != DIMS or any(abs(a - b) > 1e-4 for a, b in zip(values, vec)):
            problems.append("%s: row %d has the wrong vector" % (where, pk))
            break
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kill-after", type=int, default=40,
                    help="kill the process after this many acknowledged transactions")
    args = ap.parse_args()

    table = ("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), title VARCHAR(16), "
             "emb VECTOR(%d));" % DIMS)
    plan = make_rows(BATCHES)
    tmp = tempfile.mkdtemp(prefix="mvdb_write_")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir)
    problems = []
    driver = Driver(data_dir)
    try:
        driver.send(table)
        driver.ack()

        acknowledged = 0
        for index, rows in enumerate(plan):
            if index == args.kill_after:
                print("killing the process after %d acknowledged transactions" % acknowledged)
                driver.kill()
                for name in sorted(os.listdir(data_dir)):
                    path = os.path.join(data_dir, name)
                    print("    file %-16s %d bytes" % (name, os.path.getsize(path)))
                driver = Driver(data_dir)
                flat_survivor = [row for batch in plan[:acknowledged] for row in batch]
                problems.extend(verify(driver, flat_survivor, "after the hard kill"))
                for rest in plan[index:]:
                    driver.send("BEGIN;")
                    driver.send(insert_sql(rest))
                    driver.send("COMMIT;")
                    driver.ack()
                    driver.ack()
                    driver.ack()
                break
            driver.send("BEGIN;")
            driver.send(insert_sql(rows))
            driver.send("COMMIT;")
            driver.ack()
            driver.ack()
            driver.ack()
            acknowledged += 1

        everything = [row for batch in plan for row in batch]
        problems.extend(verify(driver, everything, "after the full load"))
        driver.close()

        driver = Driver(data_dir)
        problems.extend(verify(driver, everything, "after a clean restart"))
        driver.close()

        # and once more with a hard kill after everything was committed
        driver = Driver(data_dir)
        driver.kill()
        driver = Driver(data_dir)
        problems.extend(verify(driver, everything, "after a kill and restart"))
    finally:
        try:
            driver.close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    for problem in problems:
        print("FAIL " + problem)
    print()
    print("%d row(s) verified, %d problem(s)" % (BATCHES * BATCH, len(problems)))
    return 1 if problems else 0


def survivor_batches(plan, count):
    return plan[:count]


if __name__ == "__main__":
    sys.exit(main())
