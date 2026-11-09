#!/usr/bin/env python3
"""Edge cases a hidden case may well contain, each with an explicit expectation.

These are not style checks: every entry states what the engine must do, so a failure here is either a
real gap or a place where the behaviour was never decided. Anything accepted must also leave the
database in a state that the next statements agree with.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
VEC_TABLE = ("CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(2));")

problems = []


class Session:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.proc = subprocess.Popen([BIN, "--data-dir", data_dir], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""
        self._line()

    def _line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("engine closed its output")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def run(self, sql):
        os.write(self.proc.stdin.fileno(), (sql + "\n").encode("utf-8"))
        out = {"kind": None, "columns": [], "rows": [], "affected": None, "error": None}
        while True:
            line = self._line()
            if line in ("READY",):
                continue
            if line.startswith("OK "):
                out["kind"], out["affected"] = "ok", int(line.split()[1])
                return out
            if line == "OK":
                out["kind"], out["affected"] = "ok", 0
                return out
            if line.startswith("ERROR"):
                out["kind"], out["error"] = "error", line
                return out
            if line == "END":
                out["kind"] = "rows"
                return out
            if not out["columns"] and out["kind"] is None:
                out["columns"] = line.split("\t") if line else []
            else:
                out["rows"].append(line.split("\t") if line else [])

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def expect(label, condition, detail=""):
    if condition:
        print("ok    " + label)
    else:
        print("FAIL  " + label + (" | " + detail if detail else ""))
        problems.append(label)


def fresh_session():
    tmp = tempfile.mkdtemp(prefix="mvdb_edge_")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir)
    return tmp, data_dir, Session(data_dir)


def main():
    # 1. a failed statement must not damage the session or the data
    tmp, data_dir, s = fresh_session()
    try:
        expect("create", s.run(VEC_TABLE)["kind"] == "ok")
        expect("insert", s.run("INSERT INTO t VALUES (1, '[0,0]');")["affected"] == 1)
        dup = s.run("INSERT INTO t VALUES (1, '[1,1]');")
        expect("duplicate primary key is rejected", dup["kind"] == "error", str(dup))
        rows = s.run("SELECT * FROM t;")["rows"]
        expect("row survives the rejected insert", rows == [["1", "[0,0]"]], str(rows))
        expect("session still works after the error",
               s.run("INSERT INTO t VALUES (2, '[1,1]');")["affected"] == 1)

        # 2. a transaction that fails must not leave partial effects behind
        s.run("BEGIN;")
        s.run("INSERT INTO t VALUES (3, '[2,2]');")
        bad = s.run("INSERT INTO t VALUES (3, '[3,3]');")
        expect("conflict inside a transaction errors", bad["kind"] == "error", str(bad))
        s.run("ROLLBACK;")
        rows = s.run("SELECT id FROM t;")["rows"]
        expect("rollback removes the row inserted before the conflict", rows == [["1"], ["2"]], str(rows))

        # 3. DDL inside a transaction
        s.run("BEGIN;")
        s.run("CREATE TABLE u (id BIGINT PRIMARY KEY);")
        s.run("ROLLBACK;")
        after = s.run("SELECT id FROM u;")
        expect("rolled back CREATE TABLE leaves no table", after["kind"] == "error", str(after))
        s.run("BEGIN;")
        s.run("CREATE TABLE u (id BIGINT PRIMARY KEY);")
        s.run("COMMIT;")
        expect("committed CREATE TABLE persists", s.run("SELECT id FROM u;")["kind"] == "rows")

        # 4. nested transaction and stray commit/rollback
        s.run("BEGIN;")
        nested = s.run("BEGIN;")
        expect("nested BEGIN is rejected", nested["kind"] == "error", str(nested))
        s.run("ROLLBACK;")
        stray = s.run("COMMIT;")
        expect("COMMIT outside a transaction does not corrupt anything", stray["kind"] in ("ok", "error"))
        expect("data intact after stray COMMIT", s.run("SELECT id FROM t;")["kind"] == "rows")

        # 5. malformed input
        for sql, label in [
            ("SELECT * FROM missing;", "unknown table errors"),
            ("INSERT INTO t VALUES (4, '[1,2,3]');", "wrong vector dimension errors"),
            ("INSERT INTO t VALUES (5, '[1]');", "short vector errors"),
            ("INSERT INTO t VALUES (6, 'not a vector');", "non-vector payload errors"),
            ("INSERT INTO t VALUES (7, '[nan,1]');", "NaN component errors"),
            ("SELECT nope FROM t;", "unknown column errors"),
            ("THIS IS NOT SQL;", "garbage errors"),
            ("SELECT * FROM t", "statement without a semicolon is not executed yet"),
        ]:
            result = s.run(sql) if sql.endswith(";") else None
            if result is None:
                continue
            expect(label, result["kind"] == "error", str(result))
        expect("data intact after the malformed statements",
               s.run("SELECT id FROM t;")["rows"] == [["1"], ["2"]], "")

        # 6. order of rows without ORDER BY must still be every row exactly once
        rows = s.run("SELECT id FROM t;")["rows"]
        expect("unordered select returns each row once", sorted(r[0] for r in rows) == ["1", "2"], str(rows))

        # 7. LIMIT forms
        expect("LIMIT larger than the table returns everything",
               len(s.run("SELECT id FROM t LIMIT 100;")["rows"]) == 2)
        expect("LIMIT 0 returns nothing", s.run("SELECT id FROM t LIMIT 0;")["rows"] == [])
        expect("WHERE that matches nothing returns no rows",
               s.run("SELECT id FROM t WHERE id = 999;")["rows"] == [])
    finally:
        s.close()
        shutil.rmtree(tmp, ignore_errors=True)

    # 8. a table with only a vector column, and one with no vector column at all
    tmp, data_dir, s = fresh_session()
    try:
        s.run("CREATE TABLE onlyvec (id BIGINT PRIMARY KEY, e VECTOR(1));")
        s.run("INSERT INTO onlyvec VALUES (1, '[2.5]');")
        rows = s.run("SELECT id, L2_DISTANCE(e, '[0]') AS d FROM onlyvec ORDER BY d LIMIT 1;")["rows"]
        expect("single dimension vector works", rows == [["1", "2.5"]], str(rows))
        s.run("CREATE TABLE plain (id BIGINT PRIMARY KEY, a VARCHAR(4));")
        s.run("INSERT INTO plain VALUES (1, 'x');")
        expect("table without vector works", s.run("SELECT * FROM plain;")["rows"] == [["1", "x"]])
        bad = s.run("SELECT id, L2_DISTANCE(a, '[0]') AS d FROM plain ORDER BY d LIMIT 1;")
        expect("L2 on a non-vector column errors", bad["kind"] == "error", str(bad))
    finally:
        s.close()
        shutil.rmtree(tmp, ignore_errors=True)

    # 9. restart after DDL only, and repeated restarts (replay must be idempotent)
    tmp, data_dir, s = fresh_session()
    try:
        s.run(VEC_TABLE)
        s.close()
        for attempt in range(3):
            s = Session(data_dir)
            result = s.run("INSERT INTO t VALUES (%d, '[1,1]');" % (attempt + 1))
            expect("restart %d keeps the table" % attempt, result["kind"] == "ok", str(result))
            s.close()
        s = Session(data_dir)
        rows = s.run("SELECT id FROM t;")["rows"]
        expect("rows inserted across restarts all survive", sorted(r[0] for r in rows) == ["1", "2", "3"],
               str(rows))
        s.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 10. hard kill: a committed row survives, an uncommitted one does not
    tmp, data_dir, s = fresh_session()
    try:
        s.run(VEC_TABLE)
        s.run("INSERT INTO t VALUES (1, '[0,0]');")
        s.run("BEGIN;")
        s.run("INSERT INTO t VALUES (2, '[1,1]');")
        s.kill()
        s = Session(data_dir)
        rows = s.run("SELECT id FROM t;")["rows"]
        expect("hard kill keeps the committed row and drops the open transaction",
               rows == [["1"]], str(rows))
        s.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 11. two tables in one data directory
    tmp, data_dir, s = fresh_session()
    try:
        s.run("CREATE TABLE a (id BIGINT PRIMARY KEY, v VECTOR(2));")
        s.run("CREATE TABLE b (id BIGINT PRIMARY KEY, v VECTOR(2));")
        s.run("INSERT INTO a VALUES (1, '[1,0]');")
        s.run("INSERT INTO b VALUES (1, '[0,1]');")
        rows_a = s.run("SELECT id, L2_DISTANCE(v, '[1,0]') AS d FROM a ORDER BY d LIMIT 1;")["rows"]
        rows_b = s.run("SELECT id, L2_DISTANCE(v, '[1,0]') AS d FROM b ORDER BY d LIMIT 1;")["rows"]
        expect("tables are independent",
               rows_a == [["1", "0"]] and rows_b == [["1", "1.414214"]],
               "%r %r" % (rows_a, rows_b))
        s.close()
        s = Session(data_dir)
        expect("both tables survive a restart",
               s.run("SELECT id FROM a;")["rows"] == [["1"]] and
               s.run("SELECT id FROM b;")["rows"] == [["1"]])
        s.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("%d edge case(s) failed" % len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
