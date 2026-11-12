#!/usr/bin/env python3
"""Schema/grammar probe: table constraints from spec 2.1, INSERT column lists, multi-key ORDER BY and
statement shapes that must not be silently re-interpreted."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

BIN = "/mnt/e/mvdb/build/minivectordb"
PROBLEMS = []
CHECKS = [0]


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
                return None
            self.buf += chunk
        out, _, self.buf = self.buf.partition(b"\n")
        return out.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql, op="execute"):
        self.seq += 1
        body = {"id": self.seq, "op": op}
        if sql is not None:
            body["sql"] = sql
            body["search_mode"] = "exact"
        os.write(self.proc.stdin.fileno(), (json.dumps(body, ensure_ascii=False) + "\n").encode())
        raw = self.line()
        try:
            return json.loads(raw)
        except Exception:
            PROBLEMS.append("non-JSON reply to %r: %r" % (sql, raw))
            return {"ok": False, "error": {"code": "BAD_JSON"}}

    def close(self):
        try:
            self.req(None, op="shutdown")
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def check(name, cond, detail=""):
    CHECKS[0] += 1
    if not cond:
        PROBLEMS.append("%s  %s" % (name, detail))


def dump(reply):
    return json.dumps(reply, ensure_ascii=False)[:240]


def expect_error(name, reply):
    check(name, reply.get("ok") is False, "expected ok:false, got " + dump(reply))
    if reply.get("ok") is False:
        check(name + " code", bool((reply.get("error") or {}).get("code")), dump(reply))


def rows(reply):
    return reply.get("rows") or []


def main():
    data = tempfile.mkdtemp(prefix="mvdb-schema-")
    try:
        s = S(data)

        # --- spec 2.1: exactly one BIGINT primary key, at most one vector column per table
        expect_error("no primary key", s.req("CREATE TABLE nopk (id BIGINT, v VECTOR(2))"))
        expect_error("two primary keys", s.req(
            "CREATE TABLE twopk (a BIGINT PRIMARY KEY, b BIGINT PRIMARY KEY, v VECTOR(2))"))
        expect_error("varchar primary key", s.req(
            "CREATE TABLE vcpk (a VARCHAR(8) PRIMARY KEY, v VECTOR(2))"))
        expect_error("two vector columns", s.req(
            "CREATE TABLE twovec (id BIGINT PRIMARY KEY, a VECTOR(2), b VECTOR(2))"))
        expect_error("vector dim zero", s.req(
            "CREATE TABLE zvec (id BIGINT PRIMARY KEY, v VECTOR(0))"))
        expect_error("unknown column type", s.req(
            "CREATE TABLE badtype (id BIGINT PRIMARY KEY, v FLOAT)"))
        expect_error("duplicate column name", s.req(
            "CREATE TABLE dupcol (id BIGINT PRIMARY KEY, s VARCHAR(4), s VARCHAR(4))"))

        r = s.req("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(32), embedding VECTOR(4))")
        check("create docs", r.get("ok") is True, dump(r))
        expect_error("create existing table", s.req(
            "CREATE TABLE docs (id BIGINT PRIMARY KEY, v VECTOR(4))"))
        r = s.req("CREATE TABLE IF NOT EXISTS docs (id BIGINT PRIMARY KEY, v VECTOR(4))")
        check("if not exists is a no-op", r.get("ok") is True, dump(r))
        r = s.req("INSERT INTO docs VALUES (1,'database','[1,0,0,0]'),(2,'database','[0,1,0,0]'),"
                  "(3,'travel','[0,0,1,0]')")
        check("seed rows", r.get("ok") is True and r.get("affected_rows") == 3, dump(r))

        # --- INSERT column list must be honoured by name, not by position
        r = s.req("INSERT INTO docs (embedding, id, category) VALUES ('[0.5,0.5,0,0]', 4, 'mix')")
        check("column list insert accepted", r.get("ok") is True, dump(r))
        r = s.req("SELECT category, embedding FROM docs WHERE id=4")
        rows_ = rows(r)
        ok = r.get("ok") is True and len(rows_) == 1
        if ok:
            cat = rows_[0][0]
            vec = rows_[0][1]
            ok = cat == "mix" and isinstance(vec, list) and vec and abs(float(vec[0]) - 0.5) < 1e-6
        check("column list maps by name", ok, dump(r))

        expect_error("insert too few values", s.req("INSERT INTO docs VALUES (5,'x')"))
        expect_error("insert too many values", s.req(
            "INSERT INTO docs VALUES (5,'x','[0,0,0,0]','extra')"))
        expect_error("insert unknown column", s.req(
            "INSERT INTO docs (id, nope, embedding) VALUES (5,'x','[0,0,0,0]')"))
        expect_error("insert duplicate column", s.req(
            "INSERT INTO docs (id, id, embedding) VALUES (5,5,'[0,0,0,0]')"))
        expect_error("insert vector to varchar", s.req(
            "INSERT INTO docs VALUES (5,[0,0,0,0],'x')"))
        expect_error("insert bigint to vector", s.req(
            "INSERT INTO docs VALUES (5,'x',7)"))
        r = s.req("SELECT id FROM docs")
        check("rejected inserts left 4 rows", sorted(x[0] for x in rows(r)) == [1, 2, 3, 4], dump(r))

        # --- multi-key ORDER BY must not silently drop the second key
        r = s.req("SELECT id FROM docs ORDER BY category, id DESC")
        if r.get("ok"):
            check("multi-key order by", [x[0] for x in rows(r)] == [2, 1, 4, 3], dump(r))
        r = s.req("SELECT id FROM docs ORDER BY category ASC, id ASC LIMIT 2")
        if r.get("ok"):
            check("multi-key order by limit", [x[0] for x in rows(r)] == [1, 2], dump(r))

        # --- UPDATE shapes
        r = s.req("UPDATE docs SET category='z' WHERE id=4")
        check("update single row", r.get("ok") is True and r.get("affected_rows") == 1, dump(r))
        r = s.req("UPDATE docs SET category='z' WHERE id=99")
        check("update no match", r.get("ok") is True and r.get("affected_rows") == 0, dump(r))
        expect_error("update unknown column", s.req("UPDATE docs SET nope='z' WHERE id=4"))
        expect_error("update to duplicate pk", s.req("UPDATE docs SET id=1 WHERE id=4"))
        expect_error("update vector dim mismatch", s.req(
            "UPDATE docs SET embedding='[1,2]' WHERE id=4"))

        # --- DELETE shapes
        r = s.req("DELETE FROM docs WHERE id=99")
        check("delete no match", r.get("ok") is True and r.get("affected_rows") == 0, dump(r))
        expect_error("delete unknown column", s.req("DELETE FROM docs WHERE nope=1"))
        expect_error("delete from missing table", s.req("DELETE FROM nosuch WHERE id=1"))
        expect_error("select from missing table", s.req("SELECT * FROM nosuch"))
        expect_error("insert into missing table", s.req("INSERT INTO nosuch VALUES (1)"))

        # --- DROP TABLE is supported (a case uses it for cleanup); the table and its file go away
        check("drop table accepted", s.req("DROP TABLE docs").get("ok") is True)
        check("table gone after drop", s.req("SELECT id FROM docs").get("ok") is False)
        check("drop of a missing table fails", s.req("DROP TABLE docs").get("ok") is False)
        check("drop if exists of a missing table is a no-op",
              s.req("DROP TABLE IF EXISTS docs").get("ok") is True)
        r = s.req("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(32), "
                  "embedding VECTOR(4))")
        check("recreate after drop", r.get("ok") is True, dump(r))
        check("insert into the recreated table",
              s.req("INSERT INTO docs VALUES (1,'x','[0,0,0,0]')").get("ok") is True)
        s.close()

        # --- durability of the recreated table
        s = S(data)
        r = s.req("SELECT id FROM docs")
        check("recreated table survived restart", rows(r) == [[1]], dump(r))
        s.close()
    finally:
        shutil.rmtree(data, ignore_errors=True)

    print("checks: %d" % CHECKS[0])
    if PROBLEMS:
        print("problems: %d" % len(PROBLEMS))
        for p in PROBLEMS:
            print("  - " + p)
        return 1
    print("no problems")
    return 0


if __name__ == "__main__":
    sys.exit(main())
