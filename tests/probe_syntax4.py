#!/usr/bin/env python3
"""Fifth probe set: vector literal spellings, predicate shapes, index errors and bulk inserts."""
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
        self.line()

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 16)
            if not chunk:
                return None
            self.buf += chunk
        out, _, self.buf = self.buf.partition(b"\n")
        return out.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql=None, op="execute", raw=None):
        self.seq += 1
        if raw is not None:
            os.write(self.proc.stdin.fileno(), (raw + "\n").encode())
            return json.loads(self.line())
        body = {"id": self.seq, "op": op}
        if sql is not None:
            body["sql"] = sql
            body["search_mode"] = "exact"
        os.write(self.proc.stdin.fileno(), (json.dumps(body, ensure_ascii=False) + "\n").encode())
        return json.loads(self.line())

    def shutdown(self):
        self.req(op="shutdown")
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        return self.proc.wait(timeout=20)


def check(name, body):
    try:
        ok, detail = body()
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, "exception: %s" % exc
    RESULTS.append((name, ok, detail))
    print("%-58s %s%s" % (name, "PASS" if ok else "FAIL", "" if ok else "   <- " + str(detail)))


def main():
    def fresh():
        tmp = tempfile.mkdtemp(prefix="mvdb_p5_")
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        return tmp, data

    def literal_spellings():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(2));")
            cases = {
                "'[ 1 , 2 ]'": True,
                "'[1e-3, 2E2]'": True,
                "'[+1,+2]'": True,
                "'[1,2,]'": False,
                "'[1,,2]'": False,
                "'[1 2]'": False,
                "'[.5,-.5]'": True,
            }
            problems = []
            key = 0
            for literal, should_pass in cases.items():
                key += 1
                r = s.req("INSERT INTO t VALUES (%d, %s);" % (key, literal))
                if should_pass and r.get("ok") is not True:
                    problems.append("rejected %s (%r)" % (literal, r.get("error")))
                if not should_pass and r.get("ok") is not False:
                    problems.append("accepted %s" % literal)
            s.shutdown()
            return not problems, "; ".join(problems)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def three_term_filter():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, c VARCHAR(4), n BIGINT, e VECTOR(2));")
            s.req("INSERT INTO t VALUES (1,'a',10,'[0,0]'),(2,'a',20,'[1,1]'),(3,'b',10,'[2,2]');")
            r = s.req("SELECT id FROM t WHERE c = 'a' AND n >= 10 AND id > 0 ORDER BY id;")
            r2 = s.req("SELECT id FROM t WHERE c = 'a' AND n > 15 ORDER BY id;")
            s.shutdown()
            ok = r.get("rows") == [[1], [2]] and r2.get("rows") == [[2]]
            return ok, "three=%r two=%r" % (r.get("rows"), r2.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def index_errors():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, c VARCHAR(4));")
            missing_table = s.req("CREATE VECTOR INDEX ix ON nope (c);")
            unknown_column = s.req("CREATE VECTOR INDEX ix ON t (nope);")
            non_vector = s.req("CREATE VECTOR INDEX ix ON t (c);")
            missing_arg = s.req("CREATE VECTOR INDEX ix ON t;")
            s.shutdown()
            problems = []
            for label, r in (("missing table", missing_table), ("unknown column", unknown_column),
                             ("non-vector column", non_vector), ("missing column", missing_arg)):
                if r.get("ok") is not False:
                    problems.append(label + " accepted")
            return not problems, "; ".join(problems)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def order_by_vector_column():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(2));")
            s.req("INSERT INTO t VALUES (1,'[0,0]');")
            r = s.req("SELECT id FROM t ORDER BY e;")
            s.shutdown()
            # ordering by a vector is not part of the subset; it must be an error, not a silent no-op
            return r.get("ok") is False, "ORDER BY vector accepted: %r" % (r,)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def bulk_single_insert():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, c VARCHAR(4), e VECTOR(2));")
            values = ",".join("(%d,'c%d','[%d,0]')" % (i, i % 3, i % 5) for i in range(1, 1001))
            r = s.req("INSERT INTO t VALUES " + values + ";")
            count = s.req("SELECT id FROM t;")
            topk = s.req("SELECT id FROM t WHERE c = 'c1' ORDER BY L2_DISTANCE(e,'[0,0]') LIMIT 3;")
            s.shutdown()
            ok = (r.get("affected_rows") == 1000 and len(count.get("rows") or []) == 1000
                  and len(topk.get("rows") or []) == 3)
            return ok, "affected=%r count=%r topk=%r" % (r.get("affected_rows"),
                                                         len(count.get("rows") or []),
                                                         topk.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def escaped_json_sql():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(8));")
            # the sql field with \u escapes must decode before reaching the SQL parser
            raw = '{"id": 1, "op": "execute", "sql": "INSERT INTO t VALUES (1, \'\\u4f60\\u597d\');", "search_mode": "exact"}'
            r = s.req(raw=raw)
            got = s.req("SELECT v FROM t;")
            s.shutdown()
            ok = r.get("ok") is True and got.get("rows") == [["\u4f60\u597d"]]
            return ok, "insert=%r rows=%r" % (r.get("ok"), got.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def min_int64_key():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, v BIGINT);")
            r = s.req("INSERT INTO t VALUES (-9223372036854775808, -9223372036854775808);")
            got = s.req("SELECT id, v FROM t;")
            found = s.req("SELECT id FROM t WHERE id = -9223372036854775808;")
            s.shutdown()
            ok = (r.get("ok") is True and got.get("rows") == [[-9223372036854775808, -9223372036854775808]]
                  and found.get("rows") == [[-9223372036854775808]])
            return ok, "insert=%r rows=%r find=%r" % (r.get("ok"), got.get("rows"), found.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    for name, body in [
        ("vector literal spellings", literal_spellings),
        ("three-term AND filter", three_term_filter),
        ("CREATE VECTOR INDEX error cases", index_errors),
        ("ORDER BY a vector column is an error", order_by_vector_column),
        ("one INSERT with 1000 rows", bulk_single_insert),
        ("\\u escapes in the request JSON", escaped_json_sql),
        ("int64 minimum as a primary key", min_int64_key),
    ]:
        check(name, body)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print()
    print("%d/%d check(s) passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    for name, ok, detail in RESULTS:
        if not ok:
            print("  FAILED: " + name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
