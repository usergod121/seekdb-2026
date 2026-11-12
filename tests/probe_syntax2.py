#!/usr/bin/env python3
"""Third probe set: syntax shapes and sizes that a formal case could use without the public examples."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

BIN = "/mnt/e/mvdb/build/minivectordb"
RESULTS = []


class S:
    def __init__(self, data):
        self.proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""
        self.seq = 0
        if json.loads(self.line()) != {"type": "ready"}:
            raise RuntimeError("bad handshake")

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 16)
            if not chunk:
                raise RuntimeError("closed")
            self.buf += chunk
        out, _, self.buf = self.buf.partition(b"\n")
        return out.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql):
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": "exact"}, ensure_ascii=False) + "\n").encode())
        return json.loads(self.line())

    def shutdown(self):
        self.seq += 1
        os.write(self.proc.stdin.fileno(), (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
        self.line()
        self.proc.wait(timeout=20)


def check(name, body):
    try:
        ok, detail = body()
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, "exception: %s" % exc
    RESULTS.append((name, ok, detail))
    print("%-56s %s%s" % (name, "PASS" if ok else "FAIL", "" if ok else "   <- " + str(detail)))


def main():
    def fresh():
        tmp = tempfile.mkdtemp(prefix="mvdb_p3_")
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        return tmp, data

    def bare_vector_literal():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(2));")
            insert = s.req("INSERT INTO t VALUES (1, [1,2]);")
            query = s.req("SELECT id, L2_DISTANCE(e, [0,0]) AS d FROM t ORDER BY d LIMIT 1;")
            s.shutdown()
            return insert.get("ok") is True and query.get("rows") == [[1, 2.2360679]] or \
                (insert.get("ok") is True and abs(float((query.get("rows") or [[0, 0]])[0][1]) - 2.2360679775) < 1e-3), \
                "insert=%r query=%r" % (insert, query.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def unsupported_operators():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, v BIGINT);")
            s.req("INSERT INTO t VALUES (1, 5);")
            # IN is supported now (MySQL spells lists this way), so it is checked separately
            r_in = s.req("SELECT id FROM t WHERE id IN (1,2);")
            in_ok = (r_in.get("ok") is True and r_in.get("rows") == [[1]])
            cases = [
                     "SELECT DISTINCT id FROM t;",
                     "SELECT id + 1 FROM t;",
                     "SELECT id FROM t GROUP BY id;",
                     "SELECT id FROM t HAVING id > 0;"]
            problems = []
            for sql in cases:
                r = s.req(sql)
                if r.get("ok") is not False:
                    problems.append("accepted: " + sql)
            if not in_ok:
                problems.append("IN (1,2) did not return the matching row: %r" % (r_in,))
            s.shutdown()
            return not problems, "; ".join(problems)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def order_by_distance_without_limit():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(2));")
            values = ",".join("(%d,'[%d,%d]')" % (i, i, i) for i in range(1, 51))
            s.req("INSERT INTO t VALUES " + values + ";")
            r = s.req("SELECT id FROM t ORDER BY L2_DISTANCE(e,'[0,0]');")
            s.shutdown()
            rows = [x[0] for x in r.get("rows") or []]
            return rows == list(range(1, 51)), "first=%r count=%d" % (rows[:3], len(rows))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def long_varchar():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, s VARCHAR(4096));")
            ok = s.req("INSERT INTO t VALUES (1, '%s');" % ("a" * 4000))
            too_long = s.req("INSERT INTO t VALUES (2, '%s');" % ("a" * 4097))
            r = s.req("SELECT id FROM t;")
            s.shutdown()
            return ok.get("ok") is True and too_long.get("ok") is False and r.get("rows") == [[1]], \
                "4000=%r 4097=%r rows=%r" % (ok.get("ok"), too_long.get("ok"), r.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def unicode_identifier():
        tmp, data = fresh()
        try:
            s = S(data)
            create = s.req("CREATE TABLE \u6587\u6863 (id BIGINT PRIMARY KEY, \u5185\u5bb9 VARCHAR(8));")
            insert = s.req("INSERT INTO \u6587\u6863 VALUES (1, '\u4f60\u597d');")
            r = s.req("SELECT \u5185\u5bb9 FROM \u6587\u6863;")
            s.shutdown()
            return (create.get("ok") is True and insert.get("ok") is True
                    and r.get("rows") == [["\u4f60\u597d"]]), \
                "create=%r insert=%r rows=%r" % (create.get("ok"), insert.get("ok"), r.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def comments_and_whitespace():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("  CREATE   TABLE   t   (  id   BIGINT   PRIMARY   KEY ,  v  BIGINT  ) ;  ")
            s.req("INSERT INTO t VALUES (1, 2);   -- trailing comment")
            r = s.req("/* block */ SELECT id, v FROM t;")
            s.shutdown()
            return r.get("rows") == [[1, 2]], "rows=%r" % (r.get("rows"),)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def mixed_case_keywords():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("create table T1 (ID bigint primary key, V varchar(4), E vector(2));")
            s.req("Insert Into t1 Values (1, 'a', '[0,0]');")
            r = s.req("sElEcT id, v, l2_distance(e, '[0,0]') As d FrOm T1 OrDeR bY d dEsC lImIt 1;")
            s.shutdown()
            return r.get("rows") == [[1, "a", 0]] and r.get("columns") == ["id", "v", "d"], \
                "columns=%r rows=%r" % (r.get("columns"), r.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    for name, body in [
        ("bare [1,2] vector literal", bare_vector_literal),
        ("DISTINCT / GROUP BY / arithmetic rejected", unsupported_operators),
        ("ORDER BY distance without LIMIT over 50 rows", order_by_distance_without_limit),
        ("VARCHAR(4096) with 4000 and 4097 characters", long_varchar),
        ("unicode table and column names", unicode_identifier),
        ("comments and messy whitespace", comments_and_whitespace),
        ("mixed case keywords, tables and columns", mixed_case_keywords),
    ]:
        check(name, body)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print()
    print("%d/%d check(s) passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    for name in failed:
        print("  FAILED: " + name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
