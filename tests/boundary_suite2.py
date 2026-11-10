#!/usr/bin/env python3
"""Second boundary suite: the cases a formal set plausibly contains that suite one does not cover yet.

Written from the task specification's own wording; the public package is not touched.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

RESULTS = []


class Session:
    def __init__(self, argv):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""
        self.seq = 0
        if json.loads(self.line()) != {"type": "ready"}:
            raise RuntimeError("bad handshake")

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 16)
            if not chunk:
                raise RuntimeError("engine closed its output")
            self.buf += chunk
        out, _, self.buf = self.buf.partition(b"\n")
        return out.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql=None, op="execute", mode="exact"):
        self.seq += 1
        body = {"id": self.seq, "op": op}
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


def fake(ok, detail=""):
    return ok, detail


def run(args):
    def attach(data):
        if args.image:
            return Session(["docker", "run", "--rm", "-i", "--network=none", "--read-only",
                            "--cap-drop=ALL", "-v", "%s:/data" % data, args.image,
                            "--data-dir", "/data"])
        return Session([args.binary, "--data-dir", data])

    def fresh():
        tmp = tempfile.mkdtemp(prefix="mvdb_edge2_")
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        os.chmod(data, 0o777)
        return tmp, data

    def check(name, body):
        try:
            ok, detail = body()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, "exception: %s" % exc
        RESULTS.append((name, ok, detail))
        print("%-58s %s%s" % (name, "PASS" if ok else "FAIL", "" if ok else "   <- " + str(detail)))

    def duplicate_index_name():
        tmp, data = fresh()
        try:
            s = attach(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(2));")
            first = s.req("CREATE VECTOR INDEX ix ON t (e);")
            second = s.req("CREATE VECTOR INDEX ix ON t (e);")
            s.shutdown()
            ok = first.get("ok") is True and second.get("ok") is False and \
                (second.get("error") or {}).get("code")
            return ok, "first=%r second=%r" % (first.get("ok"), second)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def schema_validation():
        tmp, data = fresh()
        try:
            s = attach(data)
            cases = {
                "two vector columns": "CREATE TABLE a (id BIGINT PRIMARY KEY, v VECTOR(2), w VECTOR(2));",
                "VECTOR(0)": "CREATE TABLE b (id BIGINT PRIMARY KEY, v VECTOR(0));",
                "VARCHAR(-1)": "CREATE TABLE c (id BIGINT PRIMARY KEY, s VARCHAR(-1));",
                "VARCHAR primary key": "CREATE TABLE d (k VARCHAR(4) PRIMARY KEY);",
                "no primary key": "CREATE TABLE e (id BIGINT);",
            }
            problems = []
            for label, sql in cases.items():
                r = s.req(sql)
                if r.get("ok") is not False:
                    problems.append("accepted " + label)
                elif not (r.get("error") or {}).get("code"):
                    problems.append("empty code " + label)
            s.shutdown()
            return not problems, "; ".join(problems)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def float_boundaries():
        tmp, data = fresh()
        try:
            s = attach(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(3));")
            big_ok = s.req("INSERT INTO t VALUES (1, '[1e38,0,0]');")
            too_big = s.req("INSERT INTO t VALUES (2, '[1e39,0,0]');")
            tiny = s.req("INSERT INTO t VALUES (3, '[1e-45,0,0]');")
            r = s.req("SELECT id, L2_DISTANCE(e,'[0,0,0]') AS d FROM t ORDER BY id;")
            s.shutdown()
            rows = r.get("rows") or []
            ok = (big_ok.get("ok") is True and too_big.get("ok") is False and tiny.get("ok") is True
                  and len(rows) == 2)
            return ok, "1e38=%r 1e39=%r 1e-45=%r rows=%r" % (
                big_ok.get("ok"), too_big.get("ok"), tiny.get("ok"), rows)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def float32_tie_on_01():
        tmp, data = fresh()
        try:
            s = attach(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(1));")
            # 0.1 and float32(0.1) are the same stored value, so both are at distance 0 from 0.1
            s.req("INSERT INTO t VALUES (2,'[0.1]'),(3,'[0.100000001490116119384765625]'),(4,'[0]');")
            r = s.req("SELECT id, L2_DISTANCE(e,'[0.1]') AS d FROM t ORDER BY d, id;")
            s.shutdown()
            rows = r.get("rows") or []
            ok = len(rows) == 3 and [x[0] for x in rows][:2] == [2, 3] and \
                all(abs(x[1]) < 1e-9 for x in rows[:2])
            return ok, "rows=%r" % (rows,)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def insert_column_list():
        tmp, data = fresh()
        try:
            s = attach(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, name VARCHAR(5), e VECTOR(2));")
            reordered = s.req("INSERT INTO t (e, id, name) VALUES ('[1,2]', 5, 'a');")
            missing = s.req("INSERT INTO t (id, name) VALUES (6, 'b');")
            r = s.req("SELECT * FROM t;")
            s.shutdown()
            rows = r.get("rows") or []
            ok = (reordered.get("ok") is True and missing.get("ok") is False and len(rows) == 1
                  and rows[0][0] == 5 and rows[0][1] == "a"
                  and isinstance(rows[0][2], list))
            return ok, "reordered=%r missing=%r rows=%r" % (
                reordered.get("ok"), missing.get("ok"), rows)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def limit_negative():
        tmp, data = fresh()
        try:
            s = attach(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY);")
            s.req("INSERT INTO t VALUES (1),(2);")
            r = s.req("SELECT id FROM t LIMIT -1;")
            r2 = s.req("SELECT id FROM t LIMIT 1;")
            s.shutdown()
            return r.get("ok") is False and (r.get("error") or {}).get("code") and \
                len(r2.get("rows") or []) == 1, "negative=%r one=%r" % (r, r2.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def two_to_53():
        tmp, data = fresh()
        try:
            s = attach(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY);")
            r = s.req("INSERT INTO t VALUES (9007199254740992),(9007199254740993),(9007199254740991);")
            r2 = s.req("SELECT id FROM t ORDER BY id;")
            s.shutdown()
            rows = r2.get("rows") or []
            ok = r.get("affected_rows") == 3 and [x[0] for x in rows] == [9007199254740991,
                                                                         9007199254740992,
                                                                         9007199254740993]
            return ok, "affected=%r rows=%r" % (r.get("affected_rows"), rows)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def stray_commit_rollback():
        tmp, data = fresh()
        try:
            s = attach(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY);")
            commit = s.req("COMMIT;")
            rollback = s.req("ROLLBACK;")
            r = s.req("SELECT id FROM t;")
            s.shutdown()
            # either a clean error or a harmless no-op is acceptable; what must not happen is a crash
            ok = r.get("ok") is True and r.get("rows") == []
            return ok, "commit=%r rollback=%r rows=%r" % (commit.get("ok"), rollback.get("ok"),
                                                          r.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def nested_begin_keeps_outer():
        tmp, data = fresh()
        try:
            s = attach(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY);")
            s.req("BEGIN;")
            nested = s.req("BEGIN;")
            s.req("INSERT INTO t VALUES (1);")
            s.req("COMMIT;")
            r = s.req("SELECT id FROM t;")
            s.shutdown()
            ok = nested.get("ok") is False and r.get("rows") == [[1]]
            return ok, "nested=%r rows=%r (the outer transaction must survive)" % (
                nested.get("ok"), r.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def repeated_queries_stable():
        tmp, data = fresh()
        try:
            s = attach(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(2));")
            s.req("INSERT INTO t VALUES (1,'[0,0]'),(2,'[1,1]'),(3,'[2,2]');")
            query = "SELECT id, L2_DISTANCE(e,'[0,0]') AS d FROM t ORDER BY d, id LIMIT 2;"
            first = s.req(query)
            bad = s.req("INSERT INTO t VALUES (1,'[9,9]');")
            second = s.req(query)
            third = s.req(query)
            s.shutdown()
            ok = (bad.get("ok") is False and first.get("rows") == second.get("rows")
                  == third.get("rows"))
            return ok, "first=%r second=%r third=%r" % (first.get("rows"), second.get("rows"),
                                                        third.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def desc_ties_ascending_pk():
        tmp, data = fresh()
        try:
            s = attach(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(2));")
            s.req("INSERT INTO t VALUES (7,'[2,0]'),(3,'[2,0]'),(9,'[2,0]'),(1,'[0,1]');")
            asc = s.req("SELECT id FROM t ORDER BY L2_DISTANCE(e,'[0,0]') LIMIT 2;")
            desc = s.req("SELECT id FROM t ORDER BY L2_DISTANCE(e,'[0,0]') DESC LIMIT 3;")
            s.shutdown()
            ok = asc.get("rows") == [[1], [3]] and desc.get("rows") == [[3], [7], [9]]
            return ok, "asc=%r desc=%r" % (asc.get("rows"), desc.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def emoji_length_limits():
        tmp, data = fresh()
        try:
            s = attach(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, s VARCHAR(3));")
            three = s.req("INSERT INTO t VALUES (1, '\U0001F600\U0001F600\U0001F600');")
            four = s.req("INSERT INTO t VALUES (2, '\U0001F600\U0001F600\U0001F600\U0001F600');")
            r = s.req("SELECT s FROM t;")
            s.shutdown()
            rows = r.get("rows") or []
            ok = (three.get("ok") is True and four.get("ok") is False and len(rows) == 1
                  and rows[0][0] == "\U0001F600" * 3)
            return ok, "three=%r four=%r rows=%r" % (three.get("ok"), four.get("ok"), rows)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def order_by_alias_mixed_keys():
        tmp, data = fresh()
        try:
            s = attach(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, a BIGINT, b VARCHAR(5));")
            s.req("INSERT INTO t VALUES (1,2,'x'),(2,1,'y'),(3,2,'a'),(4,1,'b'),(5,2,'x');")
            r = s.req("SELECT id, a AS aa FROM t ORDER BY aa DESC, b ASC;")
            s.shutdown()
            rows = r.get("rows") or []
            want = [[3, 2], [1, 2], [5, 2], [4, 1], [2, 1]]
            return rows == want, "rows=%r want %r" % (rows, want)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def multi_statement_stops_at_error():
        tmp, data = fresh()
        try:
            s = attach(data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY);")
            r = s.req("INSERT INTO t VALUES (20); INSERT INTO t VALUES (20); INSERT INTO t VALUES (21);")
            r2 = s.req("SELECT id FROM t ORDER BY id;")
            s.shutdown()
            rows = r2.get("rows") or []
            # statement level atomicity: the first statement stays, the second fails, the third never runs
            return r.get("ok") is False and rows == [[20]], "failed=%r rows=%r" % (
                r.get("ok"), rows)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    for name, body in [
        ("duplicate index name is rejected", duplicate_index_name),
        ("schema validation: vector count, zero dim, negative length, key type", schema_validation),
        ("float32 range: 1e38 ok, 1e39 rejected, subnormal kept", float_boundaries),
        ("float32 rounding makes 0.1 a tie", float32_tie_on_01),
        ("INSERT column list is order independent, missing vector errors", insert_column_list),
        ("LIMIT -1 is an error", limit_negative),
        ("2^53+-1 keys stay distinct", two_to_53),
        ("stray COMMIT/ROLLBACK do not corrupt", stray_commit_rollback),
        ("nested BEGIN does not kill the outer transaction", nested_begin_keeps_outer),
        ("repeated identical queries stay stable", repeated_queries_stable),
        ("DESC ties still ascend by primary key", desc_ties_ascending_pk),
        ("emoji length counts code points", emoji_length_limits),
        ("ORDER BY alias with mixed DESC/ASC and key tie", order_by_alias_mixed_keys),
        ("multi-statement stops at the failing statement", multi_statement_stops_at_error),
    ]:
        check(name, body)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print()
    print("%d/%d check(s) passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    for name in failed:
        print("  FAILED: " + name)
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=None)
    ap.add_argument("--binary", default="/mnt/e/mvdb/build/minivectordb")
    sys.exit(run(ap.parse_args()))
