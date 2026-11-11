#!/usr/bin/env python3
"""Exhaustive rejection/acceptance battery.

The public package says (README): "拒绝 NaN、Infinity、维度错误、float32 溢出等非法向量", and spec 2.1
requires inputs to be finite float32 values, BIGINT values to fit and VARCHAR(n) to be measured in
Unicode characters. A formal case almost certainly probes this family, so every spelling is tried in
every position a vector can appear (insert, update, query vector of ORDER BY, projection, filtered
TopK, multi-row insert, inside a transaction, after a restart), and the accepted boundary values are
checked too. A rejected statement must also leave no partial change behind.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
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
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql, mode="exact"):
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": mode}) + "\n").encode())
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
            self.proc.wait(timeout=20)
        except Exception:
            self.proc.kill()


def check(label, condition, detail=""):
    CHECKS[0] += 1
    if not condition:
        PROBLEMS.append(label + ("  " + detail if detail else ""))
        print("FAIL  " + label + ("  " + detail if detail else ""))


def count(s):
    reply = s.req("SELECT id FROM docs")
    return len(reply.get("rows") or []) if reply.get("ok") else None


# every spelling of a vector that must be rejected, and the ones that must be accepted
BAD_VECTORS = [
    "[nan,0]", "[NaN,0]", "[NAN,0]", "[nan(1),0]",
    "[inf,0]", "[INF,0]", "[-inf,0]", "[Infinity,0]", "[-Infinity,0]", "[infinity,0]",
    "[1e39,0]", "[-1e39,0]", "[1e400,0]", "[-1e400,0]", "[3.5e38,0]", "[3.4028236e38,0]",
    "[-3.4028236e38,0]",
    "[1]", "[1,2,3]", "[]", "[1,]", "[,1]", "[1,,2]", "[1 2]", "[1;2]", "[1,2]x", "x[1,2]",
    "1,2", "", " ", "hello", "[a,b]", "[1,0x10]",
]
GOOD_VECTORS = [
    "[3.4028235e38,-3.4028235e38]", "[1e-45,-1e-45]", "[0,-0]", "[1,2]", "[ 1 , 2 ]",
    "[1.0000001,2.0000002]", "[-0.0,0.0]",
    # exactly at the float32 rounding boundary: rounds to FLT_MAX, so it is a finite value
    "[3.4028235677973366e38,0]",
]


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_reject_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        s = S(data)
        s.req("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), emb VECTOR(2))")
        s.req("INSERT INTO docs VALUES (1,'a','[1,0]'),(2,'a','[0,1]'),(3,'b','[1,1]')")
        base = count(s)

        # ---- a bad vector must be rejected in every position it can appear in
        for literal in BAD_VECTORS:
            quoted = "'%s'" % literal.replace("'", "''")
            positions = [
                ("insert %r" % literal,
                 "INSERT INTO docs VALUES (9,'z',%s)" % quoted),
                ("update %r" % literal,
                 "UPDATE docs SET emb=%s WHERE id=1" % quoted),
                ("order by %r" % literal,
                 "SELECT id FROM docs ORDER BY L2_DISTANCE(emb,%s) LIMIT 1" % quoted),
                ("projection %r" % literal,
                 "SELECT id, L2_DISTANCE(emb,%s) AS d FROM docs" % quoted),
                ("filtered topk %r" % literal,
                 "SELECT id FROM docs WHERE category='a' ORDER BY L2_DISTANCE(emb,%s) LIMIT 2"
                 % quoted),
                ("multi row %r" % literal,
                 "INSERT INTO docs VALUES (7,'z','[0,0]'),(8,'z',%s)" % quoted),
            ]
            for label, sql in positions:
                reply = s.req(sql)
                check("reject " + label, reply.get("ok") is False,
                      "accepted: " + json.dumps(reply)[:160])
                if reply.get("ok") is False:
                    check("reject " + label + " has a code",
                          bool((reply.get("error") or {}).get("code")), json.dumps(reply)[:120])
                check("no partial change after " + label, count(s) == base,
                      "rows=%r expected %r" % (count(s), base))

        # ---- bare (unquoted) spellings must be rejected the same way
        for literal in ["[nan,0]", "[1e39,0]", "[1,2,3]"]:
            for label, sql in [
                ("bare insert %r" % literal,
                 "INSERT INTO docs VALUES (9,'z',%s)" % literal),
                ("bare order by %r" % literal,
                 "SELECT id FROM docs ORDER BY L2_DISTANCE(emb,%s) LIMIT 1" % literal),
            ]:
                reply = s.req(sql)
                check("reject " + label, reply.get("ok") is False,
                      "accepted: " + json.dumps(reply)[:160])

        # ---- inside a transaction: the failure must not poison the transaction
        s.req("BEGIN")
        check("transaction open", s.req("INSERT INTO docs VALUES (20,'t','[0.5,0.5]')").get("ok") is True)
        check("bad vector inside a transaction is rejected",
              s.req("INSERT INTO docs VALUES (21,'t','[nan,0]')").get("ok") is False)
        check("bad query vector inside a transaction is rejected",
              s.req("SELECT id FROM docs ORDER BY L2_DISTANCE(emb,'[inf,0]') LIMIT 1").get("ok") is False)
        check("transaction still commits", s.req("COMMIT").get("ok") is True)
        check("committed row visible after the rejections",
              count(s) == base + 1, "%r vs %r" % (count(s), base + 1))

        # ---- accepted boundary values must be stored, and survive a restart
        for index, literal in enumerate(GOOD_VECTORS):
            quoted = "'%s'" % literal
            reply = s.req("INSERT INTO docs VALUES (%d,'g',%s)" % (100 + index, quoted))
            check("accept %r" % literal, reply.get("ok") is True, json.dumps(reply)[:160])
        before = count(s)
        s.close()
        s = S(data)
        check("accepted boundaries survived a restart", count(s) == before,
              "%r vs %r" % (count(s), before))
        for index, literal in enumerate(GOOD_VECTORS):
            reply = s.req("SELECT emb FROM docs WHERE id=%d" % (100 + index))
            check("read back %r" % literal,
                  reply.get("ok") is True and len(reply.get("rows") or []) == 1,
                  json.dumps(reply)[:160])

        # ---- BIGINT: rejected when it does not fit, accepted at the edges
        for literal in ["9223372036854775808", "-9223372036854775809", "92233720368547758080000"]:
            for label, sql in [
                ("insert %s" % literal, "INSERT INTO docs VALUES (%s,'b','[0,0]')" % literal),
                ("string insert %s" % literal, "INSERT INTO docs VALUES ('%s','b','[0,0]')" % literal),
                ("predicate %s" % literal,
                 "SELECT id FROM docs WHERE id=%s" % literal),
            ]:
                check("reject " + label, s.req(sql).get("ok") is False,
                      json.dumps(s.req(sql))[:140])
        for literal in ["9223372036854775807", "-9223372036854775808"]:
            check("accept BIGINT edge %s" % literal,
                  s.req("INSERT INTO docs VALUES (%s,'e','[0,0]')" % literal).get("ok") is True)
        check("BIGINT edges stored",
              s.req("SELECT id FROM docs ORDER BY id LIMIT 1").get("ok") is True)

        # ---- VARCHAR length counts Unicode characters, not bytes
        s.req("CREATE TABLE vc (id BIGINT PRIMARY KEY, s VARCHAR(3))")
        for text, accepted in [("\u4e2d\u6587\u5b57", True), ("\u4e2d\u6587\u5b57\u7b26", False),
                               ("abc", True), ("abcd", False),
                               ("\U0001F600\U0001F601\U0001F602", True),
                               ("\U0001F600\U0001F601\U0001F602\U0001F603", False),
                               ("", True), ("a", True)]:
            reply = s.req("INSERT INTO vc VALUES (1,'%s')" % text)
            check("VARCHAR(3) %r accepted=%s" % (text, accepted),
                  (reply.get("ok") is True) == accepted, json.dumps(reply)[:140])
            s.req("DELETE FROM vc WHERE id=1")

        # ---- and the same checks must hold after a restart
        s.close()
        s = S(data)
        check("table readable after restart", count(s) is not None)
        check("bad vector still rejected after restart",
              s.req("INSERT INTO docs VALUES (200,'x','[1e39,0]')").get("ok") is False)
        check("bad query vector still rejected after restart",
              s.req("SELECT id FROM docs ORDER BY L2_DISTANCE(emb,'[nan,0]') LIMIT 1").get("ok") is False)
        s.close()
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
    print()
    print("checks: %d" % CHECKS[0])
    if PROBLEMS:
        print("problems: %d" % len(PROBLEMS))
        for p in PROBLEMS:
            print("  - " + p)
    else:
        print("no problems")
    sys.exit(1 if PROBLEMS else 0)
