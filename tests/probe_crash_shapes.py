#!/usr/bin/env python3
"""Looks for inputs that make the engine exit before answering.

The judge reports a case as "database exited before response" when the process dies instead of
replying, so this probe tries the shapes that can kill a process: very large statements, deeply
nested expressions, big data sets across a restart, wide rows, large dimensions, odd bytes inside
string literals and thousands of result rows. Any statement after which the process is gone is a
failure; a rejection response is fine.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
FAILURES = []
CHECKS = [0]


class Engine:
    def __init__(self, data):
        self.data = data
        self.proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.buf = b""
        self.seq = 0
        if self.line() is None:
            raise RuntimeError("engine did not reach the handshake")

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 16)
            if not chunk:
                return None
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql, mode="exact"):
        if self.proc.poll() is not None:
            return None
        self.seq += 1
        payload = json.dumps({"id": self.seq, "op": "execute", "sql": sql, "search_mode": mode})
        os.write(self.proc.stdin.fileno(), (payload + "\n").encode())
        raw = self.line()
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return {"ok": False, "error": {"code": "BAD_JSON", "message": raw[:120]}}

    def close(self):
        if self.proc.poll() is None:
            try:
                self.seq += 1
                os.write(self.proc.stdin.fileno(),
                         (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
                self.line()
                self.proc.wait(timeout=120)
            except Exception:
                self.proc.kill()
        err = b""
        try:
            err = self.proc.stderr.read(600) or b""
        except Exception:
            pass
        return self.proc.returncode, err.decode("utf-8", "replace")

    def restart(self):
        code, err = self.close()
        engine = Engine(self.data)
        return engine, code, err


def check(label, condition, detail=""):
    CHECKS[0] += 1
    if not condition:
        FAILURES.append(label + ("  " + detail if detail else ""))
        print("FAIL  " + label + ("  " + detail if detail else ""))


def scenario(name, body):
    tmp = tempfile.mkdtemp(prefix="mvdb_stress_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        engine = Engine(data)
        try:
            body(engine, data)
        finally:
            code, err = engine.close()
    except Exception as exc:                      # the engine died while a scenario was running
        FAILURES.append(name + ": " + repr(exc))
        print("FAIL  " + name + "  " + repr(exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def big_table(engine, rows=60000, dims=32):
    engine.req("CREATE TABLE big (id BIGINT PRIMARY KEY, grp VARCHAR(8), emb VECTOR(%d))" % dims)
    batch = 2000
    for start in range(1, rows + 1, batch):
        values = ",".join("(%d,'g%d','[%s]')" % (
            i, i % 4, ",".join("%.3f" % (((i * (d + 1)) % 97) / 97.0) for d in range(dims)))
            for i in range(start, min(start + batch, rows + 1)))
        reply = engine.req("INSERT INTO big VALUES " + values)
        check("bulk insert of %d rows" % batch, reply is not None and reply.get("ok") is True,
              json.dumps(reply)[:120] if reply else "process gone")
        if reply is None:
            return
    check("row count", len((engine.req("SELECT id FROM big").get("rows") or [])) == rows)
    query = "[" + ",".join("0.5" for _ in range(dims)) + "]"
    reply = engine.req("SELECT id FROM big ORDER BY L2_DISTANCE(emb,'%s') LIMIT 10" % query)
    check("topk on the big table", reply is not None and len(reply.get("rows") or []) == 10)


def main():
    # 1. a big table, then a restart with a small cache budget so paging is exercised
    def big_then_restart(engine, data):
        big_table(engine, rows=20000, dims=32)
        engine2, code, err = engine.restart()
        check("restart after a big load", code == 0, "exit=%r stderr=%r" % (code, err[:200]))
        reply = engine2.req("SELECT id FROM big LIMIT 5")
        check("query after the restart", reply is not None and reply.get("ok") is True)
        # hand the restarted engine back through the outer variable
        engine.__dict__.update(engine2.__dict__)
    scenario("big table + restart", big_then_restart)

    # 2. wide rows
    def wide(engine, data):
        engine.req("CREATE TABLE w (id BIGINT PRIMARY KEY, s VARCHAR(65000), emb VECTOR(4))")
        for pk in range(1, 4):
            reply = engine.req("INSERT INTO w VALUES (%d,'%s','[1,2,3,4]')" % (pk, "x" * 60000))
            check("wide row %d" % pk, reply is not None and reply.get("ok") is True,
                  json.dumps(reply)[:120] if reply else "process gone")
        engine2, code, err = engine.restart()
        check("restart after wide rows", code == 0, "exit=%r stderr=%r" % (code, err[:200]))
        check("wide rows readable", engine2.req("SELECT id FROM w ORDER BY id").get("ok") is True)
        engine.__dict__.update(engine2.__dict__)
    scenario("wide rows + restart", wide)

    # 3. deeply nested expressions
    def nested(engine, data):
        engine.req("CREATE TABLE n (id BIGINT PRIMARY KEY, v BIGINT)")
        engine.req("INSERT INTO n VALUES (1,1)")
        for depth in (50, 500, 5000, 20000):
            sql = "SELECT id FROM n WHERE " + "(" * depth + "id = 1" + ")" * depth
            reply = engine.req(sql)
            check("nested parentheses depth %d" % depth, reply is not None,
                  "process gone at depth %d" % depth)
            if reply is None:
                return
        for depth in (50, 500, 5000):
            sql = "SELECT id FROM n WHERE " + " AND ".join(["id = 1"] * depth)
            reply = engine.req(sql)
            check("conjunction depth %d" % depth, reply is not None,
                  "process gone at depth %d" % depth)
            if reply is None:
                return
    scenario("deeply nested expressions", nested)

    # 4. one enormous INSERT statement
    def huge_insert(engine, data):
        engine.req("CREATE TABLE h (id BIGINT PRIMARY KEY, v VARCHAR(8))")
        rows = 50000
        sql = ("INSERT INTO h VALUES " +
               ",".join("(%d,'x')" % i for i in range(1, rows + 1)))
        reply = engine.req(sql)
        check("single INSERT of %d rows (%d KB)" % (rows, len(sql) // 1024),
              reply is not None and reply.get("ok") is True,
              json.dumps(reply)[:120] if reply else "process gone")
    scenario("one enormous INSERT", huge_insert)

    # 5. many columns and large dimensions
    def many_columns(engine, data):
        cols = ", ".join("c%d VARCHAR(8)" % i for i in range(400))
        reply = engine.req("CREATE TABLE m (id BIGINT PRIMARY KEY, %s)" % cols)
        check("400 columns", reply is not None and reply.get("ok") is True,
              json.dumps(reply)[:120] if reply else "process gone")
        if reply is None:
            return
        values = ",".join(["%d" % i for i in range(1, 401)])
        check("insert 400 columns",
              engine.req("INSERT INTO m VALUES (1,%s)" % values).get("ok") is True)
        reply = engine.req("SELECT * FROM m")
        check("select 400 columns", reply is not None and reply.get("ok") is True)
        reply = engine.req("CREATE TABLE v (id BIGINT PRIMARY KEY, e VECTOR(4096))")
        check("VECTOR(4096)", reply is not None and reply.get("ok") is True)
        if reply is not None and reply.get("ok"):
            vector = ",".join("0.5" for _ in range(4096))
            check("insert 4096 dimensions",
                  engine.req("INSERT INTO v VALUES (1,'[%s]')" % vector).get("ok") is True)
            reply = engine.req("SELECT id FROM v ORDER BY L2_DISTANCE(e,'[%s]') LIMIT 1" % vector)
            check("topk 4096 dimensions", reply is not None and reply.get("ok") is True)
    scenario("many columns and large dimensions", many_columns)

    # 6. odd bytes inside literals
    def odd_bytes(engine, data):
        engine.req("CREATE TABLE o (id BIGINT PRIMARY KEY, s VARCHAR(64))")
        for text in ["a\u0000b", "\u0001\u0002", "\uffff", "\u00a0", "\\", "'", "\"",
                     "line1\\nline2", "\U0001F600" * 10]:
            escaped = text.replace("\\", "\\\\").replace("'", "''")
            reply = engine.req("INSERT INTO o VALUES (1,'%s')" % escaped)
            check("odd literal %r" % text[:12], reply is not None,
                  "process gone on %r" % (text[:12],))
            if reply is None:
                return
            engine.req("DELETE FROM o WHERE id=1")
    scenario("odd bytes in literals", odd_bytes)

    # 7. thousands of result rows in one reply
    def many_rows(engine, data):
        engine.req("CREATE TABLE r (id BIGINT PRIMARY KEY, s VARCHAR(32), emb VECTOR(4))")
        for start in range(1, 6001, 1000):
            values = ",".join("(%d,'row%d','[%d,1,2,3]')" % (i, i, i % 7)
                              for i in range(start, start + 1000))
            engine.req("INSERT INTO r VALUES " + values)
        reply = engine.req("SELECT id, s, emb FROM r")
        check("6000 rows in one reply",
              reply is not None and len(reply.get("rows") or []) == 6000,
              "process gone" if reply is None else "rows=%d" % len(reply.get("rows") or []))
    scenario("thousands of rows in one reply", many_rows)

    # 8. many statements in one transaction, then a restart
    def long_transaction(engine, data):
        engine.req("CREATE TABLE lt (id BIGINT PRIMARY KEY, v VARCHAR(8))")
        engine.req("BEGIN")
        for i in range(1, 2001):
            if engine.req("INSERT INTO lt VALUES (%d,'v')" % i) is None:
                check("long transaction survived statement %d" % i, False, "process gone")
                return
        check("commit of 2000 statements", engine.req("COMMIT").get("ok") is True)
        engine2, code, err = engine.restart()
        check("restart after a long transaction", code == 0,
              "exit=%r stderr=%r" % (code, err[:200]))
        reply = engine2.req("SELECT id FROM lt ORDER BY id LIMIT 1")
        check("long transaction data survives", reply is not None and reply.get("ok") is True)
        engine.__dict__.update(engine2.__dict__)
    scenario("long transaction + restart", long_transaction)

    # 9. the same table name created, dropped and created again many times
    def churn(engine, data):
        for i in range(30):
            engine.req("CREATE TABLE ch (id BIGINT PRIMARY KEY, v VARCHAR(8))")
            engine.req("INSERT INTO ch VALUES (1,'a')")
            engine.req("DROP TABLE ch")
        engine2, code, err = engine.restart()
        check("restart after create/drop churn", code == 0, "exit=%r stderr=%r" % (code, err[:200]))
        engine.__dict__.update(engine2.__dict__)
    scenario("create/drop churn + restart", churn)

    # 10. many rows with long text: the decoded cache must not outgrow the memory budget
    def long_strings(engine, data):
        engine.req("CREATE TABLE ls (id BIGINT PRIMARY KEY, body VARCHAR(1024))")
        for start in range(1, 20001, 500):
            values = ",".join("(%d,'%s')" % (i, "y" * 1000) for i in range(start, start + 500))
            reply = engine.req("INSERT INTO ls VALUES " + values)
            if reply is None:
                check("long-string insert batch %d" % start, False, "process gone")
                return
        reply = engine.req("SELECT id FROM ls LIMIT 1")
        check("query a table of long strings", reply is not None and reply.get("ok") is True)
        reply = engine.req("SELECT body FROM ls WHERE id = 1")
        check("read a long string back",
              reply is not None and reply.get("ok") is True and
              len(((reply.get("rows") or [[None]])[0][0]) or "") == 1000)
        engine2, code, err = engine.restart()
        check("restart after long strings", code == 0, "exit=%r stderr=%r" % (code, err[:200]))
        engine.__dict__.update(engine2.__dict__)
    scenario("long strings at scale + restart", long_strings)

    print()
    print("checks: %d" % CHECKS[0])
    if FAILURES:
        print("failures: %d" % len(FAILURES))
        for item in FAILURES:
            print("  - " + item)
        return 1
    print("no failures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
