#!/usr/bin/env python3
"""Fourth probe set: semantics and limits that a hidden case could hit without the public examples."""
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

    def req(self, sql=None, op="execute", mode="exact", ident=None):
        self.seq += 1
        body = {"id": self.seq if ident is None else ident, "op": op}
        if sql is not None:
            body["sql"] = sql
            body["search_mode"] = mode
        os.write(self.proc.stdin.fileno(), (json.dumps(body, ensure_ascii=False) + "\n").encode())
        return json.loads(self.line())

    def shutdown(self):
        self.req(op="shutdown")
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        return self.proc.wait(timeout=30)


def check(name, body):
    try:
        ok, detail = body()
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, "exception: %s" % exc
    RESULTS.append((name, ok, detail))
    print("%-58s %s%s" % (name, "PASS" if ok else "FAIL" if detail else "INFO", "" if ok else "   <- " + str(detail)))


def main():
    def fresh():
        tmp = tempfile.mkdtemp(prefix="mvdb_p4_")
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        return tmp, data

    def noop_update_affected():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(8));")
            s.req("INSERT INTO t VALUES (1, 'a');")
            same = s.req("UPDATE t SET v = 'a' WHERE id = 1;")
            s.shutdown()
            # reported as information: matched-row counting (1) versus changed-row counting (0) are both
            # defensible; this records which one we do so a case expecting the other is visible early
            return True, "UPDATE with the same value reports affected_rows=%r" % (same.get("affected_rows"),)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def vector_in_where():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(2));")
            s.req("INSERT INTO t VALUES (1, '[1,2]');")
            eq = s.req("SELECT id FROM t WHERE e = '[1,2]';")
            mismatch = s.req("SELECT id FROM t WHERE L2_DISTANCE(e,'[1,2,3]') < 1;")
            s.shutdown()
            problems = []
            if eq.get("ok") is not False:
                problems.append("vector equality accepted %r" % (eq,))
            if mismatch.get("ok") is not False:
                problems.append("dimension mismatch in WHERE accepted %r" % (mismatch,))
            return not problems, "; ".join(problems)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def row_too_wide():
        """A row past the 64 KiB row limit (what MySQL itself allows) must be refused cleanly."""
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE wide (id BIGINT PRIMARY KEY, a VARCHAR(65535), b VARCHAR(4096));")
            small = s.req("INSERT INTO wide VALUES (1, '%s', '%s');" % ("x" * 60000, "y" * 10))
            r = s.req("INSERT INTO wide VALUES (2, '%s', '%s');" % ("z" * 65535, "w" * 4096))
            still_alive = s.req("SELECT id FROM wide;")
            s.shutdown()
            ok = (small.get("ok") is True and r.get("ok") is False
                  and still_alive.get("ok") is True
                  and [row[0] for row in (still_alive.get("rows") or [])] == [1])
            return ok, "small=%r insert=%r rows=%r" % (
                small.get("affected_rows"), r.get("error"), still_alive.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def many_columns():
        tmp, data = fresh()
        try:
            s = S(data)
            columns = ["c%d BIGINT" % i for i in range(1, 41)]
            create = s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, %s);" % ", ".join(columns))
            values = ",".join(str(i) for i in range(1, 41))
            insert = s.req("INSERT INTO t VALUES (1, %s);" % values)
            r = s.req("SELECT c40 FROM t;")
            s.shutdown()
            ok = (create.get("ok") is True and insert.get("ok") is True and r.get("rows") == [[40]])
            return ok, "create=%r insert=%r c40=%r" % (create.get("ok"), insert.get("ok"), r.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def large_vector():
        tmp, data = fresh()
        try:
            s = S(data)
            dims = 1024
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(%d));" % dims)
            values = ",".join("0.5" for _ in range(dims))
            insert = s.req("INSERT INTO t VALUES (1, '[%s]');" % values)
            query = s.req("SELECT id, L2_DISTANCE(e,'[%s]') AS d FROM t ORDER BY d LIMIT 1;"
                          % ",".join("0" for _ in range(dims)))
            s.shutdown()
            rows = query.get("rows") or []
            ok = insert.get("ok") is True and len(rows) == 1 and abs(rows[0][1] - (dims * 0.25) ** 0.5) < 1e-3
            return ok, "insert=%r rows=%r" % (insert.get("ok"), rows)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def empty_transaction():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY);")
            begin = s.req("BEGIN;")
            commit = s.req("COMMIT;")
            s.shutdown()
            return begin.get("ok") is True and commit.get("ok") is True, "begin=%r commit=%r" % (
                begin.get("ok"), commit.get("ok"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def transaction_with_many_statements():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, v BIGINT);")
            s.req("BEGIN;")
            for i in range(1, 301):
                r = s.req("INSERT INTO t VALUES (%d, %d);" % (i, i))
                if r.get("ok") is not True:
                    return False, "insert %d failed: %r" % (i, r)
            s.req("COMMIT;")
            r = s.req("SELECT id FROM t;")
            s.shutdown()
            return len(r.get("rows") or []) == 300, "%d rows" % len(r.get("rows") or [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def request_edge_fields():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY);")
            problems = []
            # extra unknown fields must be ignored
            s.seq += 1
            os.write(s.proc.stdin.fileno(), (json.dumps({
                "id": s.seq, "op": "execute", "sql": "INSERT INTO t VALUES (1);",
                "search_mode": "exact", "deadline_ms": 1234, "trace": "abc"}) + "\n").encode())
            r = json.loads(s.line())
            if r.get("ok") is not True:
                problems.append("extra fields rejected %r" % (r,))
            # no search_mode at all
            s.seq += 1
            os.write(s.proc.stdin.fileno(), (json.dumps({
                "id": s.seq, "op": "execute", "sql": "INSERT INTO t VALUES (2);"}) + "\n").encode())
            r = json.loads(s.line())
            if r.get("ok") is not True:
                problems.append("missing search_mode rejected %r" % (r,))
            # huge and negative ids must be echoed verbatim
            for ident in (9223372036854775807, -5, 0):
                reply = s.req("SELECT id FROM t;", ident=ident)
                if reply.get("id") != ident:
                    problems.append("id %r echoed as %r" % (ident, reply.get("id")))
            s.shutdown()
            return not problems, "; ".join(problems)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def delete_then_reinsert():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(8));")
            s.req("INSERT INTO t VALUES (1,'a'),(2,'b'),(3,'c');")
            s.req("DELETE FROM t;")
            empty = s.req("SELECT id FROM t;")
            s.req("INSERT INTO t VALUES (4,'d');")
            after = s.req("SELECT id, v FROM t;")
            s.shutdown()
            ok = empty.get("rows") == [] and after.get("rows") == [[4, "d"]]
            return ok, "empty=%r after=%r" % (empty.get("rows"), after.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def insert_value_count_mismatch():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, v BIGINT);")
            too_few = s.req("INSERT INTO t VALUES (1);")
            too_many = s.req("INSERT INTO t VALUES (1, 2, 3);")
            unknown_col = s.req("INSERT INTO t (id, nope) VALUES (1, 2);")
            update_unknown = s.req("UPDATE t SET nope = 1 WHERE id = 1;")
            s.shutdown()
            problems = []
            for label, r in (("too few", too_few), ("too many", too_many),
                             ("unknown insert column", unknown_col), ("unknown update column", update_unknown)):
                if r.get("ok") is not False:
                    problems.append(label + " accepted")
            return not problems, "; ".join(problems)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def duplicate_projections():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, v BIGINT);")
            s.req("INSERT INTO t VALUES (1, 2);")
            r = s.req("SELECT id, id, v FROM t;")
            s.shutdown()
            ok = r.get("columns") == ["id", "id", "v"] and r.get("rows") == [[1, 1, 2]]
            return ok, "columns=%r rows=%r" % (r.get("columns"), r.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def string_comparisons():
        tmp, data = fresh()
        try:
            s = S(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, s VARCHAR(8));")
            s.req("INSERT INTO t VALUES (1,'a'),(2,'b'),(3,'ab');")
            r1 = s.req("SELECT id FROM t WHERE s <> 'a' ORDER BY id;")
            r2 = s.req("SELECT id FROM t WHERE s > 'a' AND s < 'b' ORDER BY id;")
            r3 = s.req("SELECT id FROM t WHERE s = 'ab';")
            s.shutdown()
            ok = r1.get("rows") == [[2], [3]] and r2.get("rows") == [[3]] and r3.get("rows") == [[3]]
            return ok, "neq=%r range=%r eq=%r" % (r1.get("rows"), r2.get("rows"), r3.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    for name, body in [
        ("affected_rows for a same-value UPDATE", noop_update_affected),
        ("vector column in WHERE is an error", vector_in_where),
        ("row past the 64 KiB row limit is refused cleanly", row_too_wide),
        ("40 columns in one table", many_columns),
        ("VECTOR(1024) insert and distance", large_vector),
        ("empty transaction", empty_transaction),
        ("300 statements in one transaction", transaction_with_many_statements),
        ("request edge fields and id extremes", request_edge_fields),
        ("DELETE all then INSERT again", delete_then_reinsert),
        ("INSERT/UPDATE column arity and unknown columns", insert_value_count_mismatch),
        ("duplicate projections", duplicate_projections),
        ("string comparison operators", string_comparisons),
    ]:
        check(name, body)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print()
    print("%d/%d check(s) passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    for name, ok, detail in RESULTS:
        if not ok:
            print("  FAILED: " + name + ("  <- " + str(detail) if detail else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
