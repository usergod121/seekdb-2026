#!/usr/bin/env python3
"""Spec-conformance probe: finite float32 inputs, bigint string coercion, tie ordering, code-point
lengths and the "unsupported statements must not be silently re-interpreted" clause (task spec 2.1)."""
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

    def req(self, sql, mode="exact"):
        self.seq += 1
        body = {"id": self.seq, "op": "execute", "sql": sql, "search_mode": mode}
        os.write(self.proc.stdin.fileno(), (json.dumps(body, ensure_ascii=False) + "\n").encode())
        raw = self.line()
        try:
            return json.loads(raw)
        except Exception:
            PROBLEMS.append("non-JSON reply to %r: %r" % (sql, raw))
            return {"ok": False, "error": {"code": "BAD_JSON"}}

    def close(self):
        try:
            self.seq += 1
            os.write(self.proc.stdin.fileno(),
                     (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
            self.line()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def check(name, cond, detail=""):
    CHECKS[0] += 1
    if not cond:
        PROBLEMS.append("%s  %s" % (name, detail))


def ids(reply):
    if not reply.get("ok"):
        return None
    cols = reply.get("columns") or []
    rows = reply.get("rows") or []
    if "id" not in cols:
        return None
    i = cols.index("id")
    return [row[i] for row in rows]


def scalar(reply):
    rows = reply.get("rows") or []
    if not reply.get("ok") or not rows or not rows[0]:
        return None
    return rows[0][0]


def expect_error(name, reply):
    check(name, reply.get("ok") is False,
          "expected ok:false, got %s" % json.dumps(reply)[:220])
    if reply.get("ok") is False:
        err = reply.get("error") or {}
        check(name + " error.code", bool(err.get("code")), "empty error code")


def main():
    data = tempfile.mkdtemp(prefix="mvdb-finite-")
    try:
        s = S(data)
        s.req("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(32), embedding VECTOR(4))")
        r = s.req("INSERT INTO docs VALUES (1,'database','[1,0,0,0]'),(2,'database','[0,1,0,0]'),"
                  "(3,'travel','[0,0,1,0]')")
        check("setup insert", r.get("ok") is True and r.get("affected_rows") == 3, json.dumps(r)[:200])

        # --- 2.1: inputs must be finite float32 (no NaN, no Infinity, no float conversion overflow)
        expect_error("insert 1e39 element", s.req(
            "INSERT INTO docs VALUES (4,'x','[1e39,0,0,0]')"))
        expect_error("insert -1e39 element", s.req(
            "INSERT INTO docs VALUES (4,'x','[-1e39,0,0,0]')"))
        expect_error("insert nan element", s.req(
            "INSERT INTO docs VALUES (4,'x','[nan,0,0,0]')"))
        expect_error("insert inf element", s.req(
            "INSERT INTO docs VALUES (4,'x','[inf,0,0,0]')"))
        expect_error("insert 1e400 element", s.req(
            "INSERT INTO docs VALUES (4,'x','[1e400,0,0,0]')"))
        expect_error("update 1e39 element", s.req(
            "UPDATE docs SET embedding='[1e39,0,0,0]' WHERE id=1"))
        expect_error("query vector 1e39", s.req(
            "SELECT id FROM docs ORDER BY L2_DISTANCE(embedding,'[1e39,0,0,0]') LIMIT 1"))
        expect_error("query vector nan", s.req(
            "SELECT id FROM docs ORDER BY L2_DISTANCE(embedding,'[nan,0,0,0]') LIMIT 1"))
        expect_error("query vector inf", s.req(
            "SELECT id FROM docs ORDER BY L2_DISTANCE(embedding,'[inf,1,0,0]') LIMIT 1"))
        expect_error("projection vector nan", s.req(
            "SELECT id, L2_DISTANCE(embedding,'[nan,0,0,0]') AS d FROM docs"))
        expect_error("bare literal overflow", s.req(
            "SELECT id FROM docs ORDER BY L2_DISTANCE(embedding,[1e39,0,0,0]) LIMIT 1"))

        # a finite float32 extreme must still be accepted
        r = s.req("INSERT INTO docs VALUES (5,'x','[3.4e38,-3.4e38,0,0]')")
        check("float32 max accepted", r.get("ok") is True, json.dumps(r)[:200])
        r = s.req("INSERT INTO docs VALUES (6,'x','[1e-50,0,0,0]')")
        check("underflow accepted", r.get("ok") is True, json.dumps(r)[:200])
        for extra in (5, 6):
            s.req("DELETE FROM docs WHERE id=%d" % extra)
        r = s.req("SELECT id FROM docs")
        check("docs back to 3 rows", ids(r) == [1, 2, 3], json.dumps(r)[:200])

        # --- 2.1: dimension mismatch on query
        expect_error("query dim mismatch", s.req(
            "SELECT id FROM docs ORDER BY L2_DISTANCE(embedding,'[1,2,3]') LIMIT 1"))
        expect_error("projection dim mismatch", s.req(
            "SELECT id, L2_DISTANCE(embedding,'[1,2,3,4,5]') AS d FROM docs"))

        # --- bigint string coercion must reject overflow instead of clamping
        expect_error("bigint string overflow", s.req(
            "INSERT INTO docs VALUES ('9223372036854775808','x','[0,0,0,0]')"))
        expect_error("bigint string underflow", s.req(
            "INSERT INTO docs VALUES ('-9223372036854775809','x','[0,0,0,0]')"))
        r = s.req("INSERT INTO docs VALUES ('7','x','[0,0,0,0]')")
        check("bigint string in range", r.get("ok") is True, json.dumps(r)[:200])
        r = s.req("DELETE FROM docs WHERE id=7")
        check("cleanup id 7", r.get("ok") is True and r.get("affected_rows") == 1, json.dumps(r)[:200])

        # --- 2.2: plain ORDER BY ties are ordered by primary key ascending
        r = s.req("SELECT id FROM docs ORDER BY category LIMIT 2")
        check("order by ties -> pk asc", ids(r) == [1, 2], json.dumps(r)[:220])
        r = s.req("SELECT id FROM docs ORDER BY category DESC LIMIT 2")
        # spec 2.2: ties inside a value group are ordered by primary key ascending, so 'database'
        # yields id 1 before id 2 even though the group itself sorts after 'travel'.
        check("order by ties desc -> pk asc within group", ids(r) == [3, 1],
              json.dumps(r)[:220])
        # equal distances -> primary key ascending
        s.req("INSERT INTO docs VALUES (10,'t','[1,0,0,0]'),(9,'t','[1,0,0,0]'),(8,'t','[1,0,0,0]')")
        r = s.req("SELECT id FROM docs ORDER BY L2_DISTANCE(embedding,'[1,0,0,0]') LIMIT 3")
        check("topk ties -> pk asc", ids(r) == [1, 8, 9], json.dumps(r)[:220])
        r = s.req("SELECT id FROM docs WHERE category='t' "
                  "ORDER BY L2_DISTANCE(embedding,'[1,0,0,0]') LIMIT 2", mode="ann")
        check("ann filtered ties -> pk asc", ids(r) == [8, 9], json.dumps(r)[:220])
        s.req("DELETE FROM docs WHERE category='t'")

        # --- 2.3: statement failure leaves no partial modification
        r = s.req("INSERT INTO docs VALUES (11,'t','[0,0,0,0]'),(1,'dup','[0,0,0,0]')")
        expect_error("multi-row insert with duplicate pk", r)
        r = s.req("SELECT id FROM docs WHERE id=11")
        check("failed multi-row insert left nothing", ids(r) == [], json.dumps(r)[:200])

        # --- 2.1: unsupported constructs must error or be executed correctly, never re-interpreted
        r = s.req("SELECT COUNT(*) FROM docs")
        if r.get("ok"):
            check("count(*)", scalar(r) == 3, json.dumps(r)[:200])
        r = s.req("SELECT category, COUNT(*) FROM docs GROUP BY category")
        if r.get("ok"):
            check("group by", len(r.get("rows") or []) == 2, json.dumps(r)[:200])
        expect_error("join", s.req("SELECT a.id FROM docs a JOIN docs b ON a.id=b.id"))
        r = s.req("SELECT id FROM docs WHERE id IN (1,3)")
        if r.get("ok"):
            check("in list", sorted(ids(r)) == [1, 3], json.dumps(r)[:200])
        r = s.req("SELECT id FROM docs WHERE id BETWEEN 1 AND 2")
        if r.get("ok"):
            check("between", sorted(ids(r)) == [1, 2], json.dumps(r)[:200])
        r = s.req("SELECT id FROM docs WHERE category LIKE 'dat%'")
        if r.get("ok"):
            check("like", sorted(ids(r)) == [1, 2], json.dumps(r)[:200])
        r = s.req("SELECT id FROM docs WHERE category IS NULL")
        if r.get("ok"):
            check("is null", ids(r) == [], json.dumps(r)[:200])
        expect_error("subquery", s.req("SELECT id FROM (SELECT id FROM docs) x"))
        expect_error("scalar subquery", s.req("SELECT (SELECT 1) AS x FROM docs"))
        r = s.req("SELECT DISTINCT category FROM docs")
        if r.get("ok"):
            check("distinct", len(r.get("rows") or []) == 2, json.dumps(r)[:200])
        expect_error("alter table", s.req("ALTER TABLE docs ADD COLUMN x BIGINT"))
        expect_error("create as select", s.req("CREATE TABLE t2 AS SELECT * FROM docs"))
        expect_error("insert select", s.req("INSERT INTO t2 SELECT * FROM docs"))
        expect_error("with cte", s.req("WITH c AS (SELECT 1) SELECT * FROM c"))
        expect_error("union", s.req("SELECT id FROM docs UNION SELECT id FROM docs"))
        expect_error("drop column", s.req("ALTER TABLE docs DROP COLUMN category"))
        expect_error("savepoint", s.req("SAVEPOINT sp1"))
        expect_error("grant", s.req("GRANT ALL ON *.* TO u"))
        expect_error("select without from", s.req("SELECT 1"))
        r = s.req("BEGIN")
        check("begin ok", r.get("ok") is True, json.dumps(r)[:160])
        expect_error("nested begin", s.req("BEGIN"))
        r = s.req("ROLLBACK")
        check("rollback ok", r.get("ok") is True, json.dumps(r)[:160])
        r = s.req("SELECT id FROM docs")
        check("table survived rejected statements", ids(r) is not None and len(ids(r)) == 3,
              json.dumps(r)[:200])

        # --- 2.1: VARCHAR length counts Unicode code points
        r = s.req("CREATE TABLE u (id BIGINT PRIMARY KEY, s VARCHAR(2))")
        check("create u", r.get("ok") is True, json.dumps(r)[:160])
        r = s.req("INSERT INTO u VALUES (1,'\u4e2d\u6587')")
        check("2 code points accepted", r.get("ok") is True, json.dumps(r)[:160])
        expect_error("3 code points rejected", s.req("INSERT INTO u VALUES (2,'\u4e2d\u6587A')"))
        r = s.req("INSERT INTO u VALUES (3,'\u00e9\u00e9')")
        check("accented 2 code points", r.get("ok") is True, json.dumps(r)[:160])
        expect_error("4-byte code point overflow",
                     s.req("INSERT INTO u VALUES (4,'\U0001F600\U0001F600\U0001F600')"))

        # --- autocommit semantics for unqualified UPDATE / DELETE
        r = s.req("UPDATE docs SET category='all'")
        check("update without where", r.get("ok") is True and r.get("affected_rows") == 3,
              json.dumps(r)[:200])
        r = s.req("DELETE FROM docs")
        check("delete without where", r.get("ok") is True and r.get("affected_rows") == 3,
              json.dumps(r)[:200])

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
