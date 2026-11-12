#!/usr/bin/env python3
"""Boundary and extreme-case suite, written from the task specification (not from the public cases).

Each check states the behaviour the specification requires, so a failure is either a real gap or a place
where the behaviour was never decided. The public package is untouched: this speaks the published JSON
protocol directly, and with --image it runs inside the judge's own container shape.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

RESULTS = []


class Session:
    def __init__(self, argv, env=None):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0, env=env)
        self.buf = b""
        self.seq = 0
        head = self.line()
        if json.loads(head) != {"type": "ready"}:
            raise RuntimeError("bad handshake: %r" % head)

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 16)
            if not chunk:
                raise RuntimeError("engine closed its output")
            self.buf += chunk
        out, _, self.buf = self.buf.partition(b"\n")
        return out.decode("utf-8", "replace").rstrip("\r")

    def raw(self, payload):
        os.write(self.proc.stdin.fileno(), (payload + "\n").encode("utf-8"))
        return json.loads(self.line())

    def req(self, sql=None, op="execute", mode="exact", ident=None):
        self.seq += 1
        body = {"id": self.seq if ident is None else ident, "op": op}
        if sql is not None:
            body["sql"] = sql
            body["search_mode"] = mode
        reply = self.raw(json.dumps(body, ensure_ascii=False))
        return reply

    def shutdown(self):
        reply = self.req(op="shutdown")
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        code = self.proc.wait(timeout=30)
        return reply, code, self.buf + self.proc.stdout.read()

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()


def attach(args, data_dir, extra_env=None):
    if args.image:
        argv = ["docker", "run", "--rm", "-i", "--network=none", "--read-only", "--cap-drop=ALL",
                "--cpus=2", "--memory=2g", "--pids-limit=128"]
        for key, value in (extra_env or {}).items():
            argv += ["-e", "%s=%s" % (key, value)]
        argv += ["-v", "%s:/data" % data_dir, args.image, "--data-dir", "/data"]
        return Session(argv)
    env = dict(os.environ, **(extra_env or {}))
    return Session([args.binary, "--data-dir", data_dir], env=env)


def check(name, body):
    try:
        ok, detail = body()
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, "exception: %s" % exc
    RESULTS.append((name, ok, detail))
    print("%-58s %s%s" % (name, "PASS" if ok else "FAIL", "" if ok else "   <- " + str(detail)))


def run(args):
    def fresh(extra_env=None, tmp_holder=None):
        tmp = tempfile.mkdtemp(prefix="mvdb_edge_")
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        os.chmod(data, 0o777)
        return tmp, data

    # ---------------- SQL and storage boundaries ----------------

    def varchar_counts_code_points():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(4));")
            sixteen = "一" * 4
            r = s.req("INSERT INTO t VALUES (1, '%s');" % sixteen)
            if r.get("ok") is not True:
                return False, "4 code points rejected in VARCHAR(4)"
            r = s.req("INSERT INTO t VALUES (2, '%s');" % ("一" * 5))
            if r.get("ok") is not False:
                return False, "5 code points accepted in VARCHAR(4)"
            r = s.req("SELECT v FROM t;")
            s.shutdown()
            return r.get("rows") == [[sixteen]], "rows=%r" % (r.get("rows"),)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def emoji_and_escapes():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(16));")
            payloads = ["emoji 😀", "quote ' inside", "back\\slash", "semi ; colon", "tab\there"]
            for index, value in enumerate(payloads, 1):
                literal = value.replace("'", "''")
                r = s.req("INSERT INTO t VALUES (%d, '%s');" % (index, literal))
                if r.get("ok") is not True:
                    return False, "insert %r rejected" % value
            r = s.req("SELECT id, v FROM t ORDER BY id;")
            s.shutdown()
            rows = r.get("rows") or []
            got = [row[1] for row in rows]
            if got != payloads:
                return False, "got %r" % (got,)
            return True, ""
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def int64_extremes():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, v BIGINT);")
            values = [("9223372036854775807", "9223372036854775807"),
                      ("-9223372036854775808", "-9223372036854775808"),
                      ("9007199254740993", "-9007199254740993")]
            for index, (pk, value) in enumerate(values, 1):
                r = s.req("INSERT INTO t VALUES (%s, %s);" % (pk, value))
                if r.get("ok") is not True:
                    return False, "insert %s rejected" % pk
            r = s.req("INSERT INTO t VALUES (7, 9223372036854775808);")
            overflow_rejected = r.get("ok") is False and (r.get("error") or {}).get("code")
            r2 = s.req("SELECT id, v FROM t ORDER BY id;")
            s.shutdown()
            rows = r2.get("rows") or []
            want = [[int(pk), int(value)] for pk, value in sorted(values, key=lambda p: int(p[0]))]
            if rows != want:
                return False, "rows=%r" % (rows,)
            return bool(overflow_rejected), "int64 overflow literal accepted (got %r)" % (r,)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def unsupported_forms_error():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE a (id BIGINT PRIMARY KEY, v BIGINT);")
            s.req("CREATE TABLE b (id BIGINT PRIMARY KEY, v BIGINT);")
            s.req("INSERT INTO a VALUES (1, 1);")
            cases = [
                "SELECT * FROM a JOIN b ON a.id = b.id;",
                "SELECT COUNT(*) FROM a;",
                "SELECT MAX(v) FROM a;",
                "SELECT * FROM a WHERE id IN (SELECT id FROM b);",
                "ALTER TABLE a ADD extra BIGINT;",
                "INSERT INTO a VALUES (NULL, 1);",
                "UPDATE a SET id = 2 WHERE id = 1;",
                "CREATE INDEX idx ON a (v);",
            ]
            problems = []
            for sql in cases:
                r = s.req(sql)
                if r.get("ok") is not False:
                    problems.append("accepted: " + sql)
                elif not (r.get("error") or {}).get("code"):
                    problems.append("empty code: " + sql)
            # nothing may have changed
            r = s.req("SELECT id, v FROM a;")
            s.shutdown()
            if r.get("rows") != [[1, 1]]:
                problems.append("state changed: %r" % (r.get("rows"),))
            return not problems, "; ".join(problems)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def nested_transaction_and_stray_control():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, n BIGINT);")
            s.req("BEGIN;")
            r = s.req("BEGIN;")
            nested = r.get("ok") is False
            s.req("INSERT INTO t VALUES (1);")
            s.req("ROLLBACK;")
            r2 = s.req("SELECT id FROM t;")
            stray = s.req("COMMIT;")
            s.shutdown()
            ok = nested and r2.get("rows") == [] and stray.get("ok") is True
            return ok, "nested=%r rows=%r stray_commit=%r" % (nested, r2.get("rows"), stray)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def code_point_ordering():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(8));")
            s.req("INSERT INTO t VALUES (1, '\u7532'), (2, '\u4e59'), (3, 'A'), (4, 'a');")
            r = s.req("SELECT id FROM t ORDER BY v;")
            s.shutdown()
            rows = [row[0] for row in r.get("rows") or []]
            return rows == [3, 4, 2, 1], "order=%r (expected A, a, 乙, 甲)" % (rows,)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- result shape ----------------

    def select_star_with_vector():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(4), e VECTOR(2));")
            s.req("INSERT INTO t VALUES (1, 'x', '[1.5,-2]');")
            r = s.req("SELECT * FROM t;")
            s.shutdown()
            rows = r.get("rows")
            ok = (r.get("columns") == ["id", "v", "e"] and isinstance(rows, list) and len(rows) == 1
                  and rows[0][0] == 1 and rows[0][1] == "x" and isinstance(rows[0][2], list)
                  and abs(rows[0][2][0] - 1.5) < 1e-6 and abs(rows[0][2][1] + 2) < 1e-6)
            return ok, "columns=%r rows=%r" % (r.get("columns"), rows)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def empty_queries_and_limits():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, n BIGINT);")
            problems = []
            r = s.req("SELECT id FROM t;")
            if r.get("columns") != ["id"] or r.get("rows") != []:
                problems.append("empty select %r" % (r,))
            s.req("INSERT INTO t VALUES (1,10),(2,20),(3,30);")
            if s.req("SELECT id FROM t LIMIT 0;").get("rows") != []:
                problems.append("LIMIT 0")
            if len(s.req("SELECT id FROM t LIMIT 99;").get("rows") or []) != 3:
                problems.append("LIMIT 99")
            if s.req("SELECT id FROM t WHERE id > 99;").get("rows") != []:
                problems.append("no-match filter")
            r = s.req("UPDATE t SET n = 5 WHERE id > 99;")
            if r.get("affected_rows") != 0:
                problems.append("update affected %r" % (r.get("affected_rows"),))
            r = s.req("DELETE FROM t WHERE id > 99;")
            if r.get("affected_rows") != 0:
                problems.append("delete affected %r" % (r.get("affected_rows"),))
            s.shutdown()
            return not problems, "; ".join(problems)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def alias_ordering_and_multi_key():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, c VARCHAR(8), e VECTOR(2));")
            s.req("INSERT INTO t VALUES (5,'b','[1,0]'),(3,'a','[2,0]'),(1,'a','[3,0]'),(9,'a','[0,0]');")
            by_alias = s.req("SELECT id, L2_DISTANCE(e,'[0,0]') AS d FROM t ORDER BY d LIMIT 2;")
            multi = s.req("SELECT id FROM t ORDER BY c, id DESC;")
            desc = s.req("SELECT id FROM t ORDER BY L2_DISTANCE(e,'[0,0]') DESC LIMIT 2;")
            s.shutdown()
            problems = []
            if by_alias.get("rows") != [[9, 0], [5, 1]]:
                problems.append("alias order %r" % (by_alias.get("rows"),))
            if multi.get("rows") != [[9], [3], [1], [5]]:
                problems.append("multi key %r" % (multi.get("rows"),))
            if desc.get("rows") != [[1], [3]]:
                problems.append("desc %r" % (desc.get("rows"),))
            return not problems, "; ".join(problems)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- vector boundaries ----------------

    def vector_dimension_one():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(1));")
            s.req("INSERT INTO t VALUES (1, '[2.5]'), (2, '[-1]');")
            r = s.req("SELECT id, L2_DISTANCE(e,'[0]') AS d FROM t ORDER BY d;")
            s.shutdown()
            return r.get("rows") == [[2, 1], [1, 2.5]], "rows=%r" % (r.get("rows"),)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def vector_128_dims():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(128));")
            zeros = ",".join(["0"] * 128)
            s.req("INSERT INTO t VALUES (1, '[%s]');" % zeros)
            ones = ",".join(["1"] * 128)
            s.req("INSERT INTO t VALUES (2, '[%s]');" % ones)
            r = s.req("SELECT id, L2_DISTANCE(e,'[%s]') AS d FROM t ORDER BY d LIMIT 2;" % zeros)
            s.shutdown()
            rows = r.get("rows") or []
            ok = (len(rows) == 2 and rows[0][0] == 1 and abs(rows[0][1]) < 1e-6
                  and abs(rows[1][1] - (128 ** 0.5)) < 1e-3)
            return ok, "rows=%r" % (rows,)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def topk_traps():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, c VARCHAR(4), e VECTOR(2));")
            # duplicated vectors -> ties on distance; two different categories
            s.req("INSERT INTO t VALUES (4,'k','[0,0]'),(2,'k','[0,0]'),(6,'s','[0,0]'),"
                  "(8,'k','[1,0]'),(1,'k','[2,0]');")
            problems = []
            r = s.req("SELECT id FROM t WHERE c = 'k' ORDER BY L2_DISTANCE(e,'[0,0]') LIMIT 2;")
            if r.get("rows") != [[2], [4]]:
                problems.append("filtered ties %r" % (r.get("rows"),))
            r = s.req("SELECT id FROM t WHERE c = 'k' ORDER BY L2_DISTANCE(e,'[0,0]') LIMIT 99;")
            if [row[0] for row in r.get("rows") or []] != [2, 4, 8, 1]:
                problems.append("filtered all %r" % (r.get("rows"),))
            r = s.req("SELECT id FROM t WHERE c = 'zz' ORDER BY L2_DISTANCE(e,'[0,0]') LIMIT 2;")
            if r.get("rows") != []:
                problems.append("empty filter %r" % (r.get("rows"),))
            r = s.req("SELECT id FROM t ORDER BY L2_DISTANCE(e,'[0,0]') LIMIT 0;")
            if r.get("rows") != []:
                problems.append("limit 0")
            s.shutdown()
            return not problems, "; ".join(problems)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def ann_matches_exact_on_ties():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, c VARCHAR(4), e VECTOR(2));")
            s.req("INSERT INTO t VALUES (3,'a','[0,0]'),(1,'a','[0,0]'),(2,'b','[9,9]');")
            s.req("CREATE VECTOR INDEX ix ON t (e);")
            query = "SELECT id FROM t WHERE c = 'a' ORDER BY L2_DISTANCE(e,'[0,0]') LIMIT 2;"
            exact = s.req(query)
            ann = s.req(query, mode="ann")
            s.shutdown()
            return exact.get("rows") == ann.get("rows") == [[1], [3]], \
                "exact=%r ann=%r" % (exact.get("rows"), ann.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- transactions and recovery ----------------

    def transaction_sees_own_changes():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(2));")
            s.req("BEGIN;")
            s.req("INSERT INTO t VALUES (1,'[0,0]');")
            r = s.req("SELECT id, L2_DISTANCE(e,'[0,0]') AS d FROM t ORDER BY d;")
            s.req("ROLLBACK;")
            r2 = s.req("SELECT id FROM t;")
            s.shutdown()
            ok = r.get("rows") == [[1, 0]] and r2.get("rows") == []
            return ok, "inside=%r after=%r" % (r.get("rows"), r2.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def error_then_continue_in_transaction():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(4));")
            s.req("BEGIN;")
            s.req("INSERT INTO t VALUES (1,'a');")
            bad = s.req("INSERT INTO t VALUES (1,'b');")
            s.req("INSERT INTO t VALUES (2,'c');")
            s.req("COMMIT;")
            r = s.req("SELECT id, v FROM t ORDER BY id;")
            s.shutdown()
            ok = bad.get("ok") is False and r.get("rows") == [[1, "a"], [2, "c"]]
            return ok, "bad=%r rows=%r" % (bad.get("ok"), r.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def commit_survives_kill_and_recovery_kill():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, v BIGINT);")
            for i in range(1, 41):
                if i % 10 == 1:
                    s.req("BEGIN;")
                s.req("INSERT INTO t VALUES (%d, %d);" % (i, i * 2))
                if i % 10 == 0:
                    s.req("COMMIT;")
            s.kill()
            for _ in range(2):
                proc = subprocess.Popen([a for a in (["docker", "run", "--rm", "-i", "--network=none",
                                                     "--cap-drop=ALL", "-v", "%s:/data" % data,
                                                     args.image, "--data-dir", "/data"]
                                                    if args.image else [])] or
                                        [args.binary, "--data-dir", data],
                                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
                time.sleep(0.05)
                proc.send_signal(signal.SIGKILL)
                proc.wait()
            s = attach(args, data)
            r = s.req("SELECT id FROM t;")
            s.shutdown()
            rows = sorted(int(x[0]) for x in r.get("rows") or [])
            return rows == list(range(1, 41)), "%d rows" % len(rows)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- protocol boundaries ----------------

    def protocol_id_types_and_errors():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            problems = []
            # string id must be echoed as a string
            reply = s.raw(json.dumps({"id": "req-7", "op": "execute", "sql": "SELECT 1;"}))
            if reply.get("id") != "req-7" or not isinstance(reply.get("id"), str):
                problems.append("string id echoed as %r" % (reply.get("id"),))
            # unknown op
            reply = s.raw(json.dumps({"id": 42, "op": "frobnicate"}))
            if reply.get("ok") is not False or not (reply.get("error") or {}).get("code"):
                problems.append("unknown op %r" % (reply,))
            # missing sql
            reply = s.raw(json.dumps({"id": 43, "op": "execute"}))
            if reply.get("ok") is not False:
                problems.append("missing sql accepted")
            # malformed json line must not kill the process
            reply = s.raw("{not json")
            if reply.get("ok") is not False:
                problems.append("malformed json %r" % (reply,))
            # still alive and correct afterwards
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY);")
            reply = s.req("SELECT id FROM t;")
            if reply.get("rows") != []:
                problems.append("after protocol errors %r" % (reply,))
            s.shutdown()
            return not problems, "; ".join(problems)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def shutdown_is_silent_and_zero():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY);")
            reply, code, extra = s.shutdown()
            ok = reply.get("ok") is True and code == 0 and extra == b""
            return ok, "ok=%r exit=%r extra=%r" % (reply.get("ok"), code, extra[:60])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def stdin_eof_exits_zero():
        tmp, data = fresh()
        try:
            argv = ["docker", "run", "--rm", "-i", "--network=none", "-v", "%s:/data" % data,
                    args.image, "--data-dir", "/data"] if args.image else [args.binary, "--data-dir", data]
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            out, _ = proc.communicate(timeout=60)
            lines = [l for l in out.decode().splitlines() if l.strip()]
            return proc.returncode == 0 and lines and json.loads(lines[0]) == {"type": "ready"}, \
                "exit=%r lines=%r" % (proc.returncode, lines[:2])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- scale / durability ----------------

    def bulk_then_restart():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, c VARCHAR(4), e VECTOR(4));")
            for start in range(0, 2000, 200):
                values = ",".join("(%d,'c%d','[%d,0,0,0]')" % (i + 1, (i + 1) % 5, (i + 1) % 3)
                                  for i in range(start, start + 200))
                s.req("INSERT INTO t VALUES " + values + ";")
            r = s.req("SELECT id FROM t WHERE c = 'c0' ORDER BY id LIMIT 1000;")
            first = len(r.get("rows") or [])
            reply, code, _ = s.shutdown()
            s = attach(args, data)
            r2 = s.req("SELECT id FROM t;")
            r3 = s.req("SELECT id FROM t WHERE c = 'c0' ORDER BY L2_DISTANCE(e,'[0,0,0,0]') LIMIT 3;")
            s.shutdown()
            ok = (code == 0 and len(r2.get("rows") or []) == 2000 and first == 400
                  and len(r3.get("rows") or []) == 3)
            return ok, "count=%r filtered=%r topk=%r" % (len(r2.get("rows") or []), first,
                                                         len(r3.get("rows") or []))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def index_maintenance_interleaved():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(2));")
            s.req("INSERT INTO t VALUES (1,'[0,0]'),(2,'[1,0]'),(3,'[2,0]');")
            s.req("CREATE VECTOR INDEX ix ON t (e);")
            s.req("UPDATE t SET e = '[9,9]' WHERE id = 1;")
            s.req("DELETE FROM t WHERE id = 2;")
            s.req("INSERT INTO t VALUES (4,'[0.5,0]');")
            r = s.req("SELECT id, L2_DISTANCE(e,'[0,0]') AS d FROM t ORDER BY d;", mode="ann")
            s.shutdown()
            rows = r.get("rows") or []
            return rows == [[4, 0.5], [3, 2], [1, 12.727922061357855]] or \
                (len(rows) == 3 and rows[0][0] == 4 and rows[1][0] == 3 and rows[2][0] == 1), \
                "rows=%r" % (rows,)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def create_table_edges():
        tmp, data = fresh()
        try:
            s = attach(args, data)
            s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(2));")
            problems = []
            if s.req("CREATE TABLE t (id BIGINT PRIMARY KEY);").get("ok") is not False:
                problems.append("duplicate create accepted")
            if s.req("CREATE VECTOR INDEX ix ON t (id);").get("ok") is not False:
                problems.append("index on a non-vector column accepted")
            if s.req("CREATE TABLE t2 (id BIGINT);").get("ok") is not False:
                problems.append("table without primary key accepted")
            if s.req("CREATE TABLE t3 (id BIGINT PRIMARY KEY, a VECTOR(2), b VECTOR(2));").get("ok") is not False:
                problems.append("two vector columns accepted")
            s.shutdown()
            return not problems, "; ".join(problems)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    for name, body in [
        ("VARCHAR length counts code points", varchar_counts_code_points),
        ("emoji, quotes, semicolons, tabs round-trip", emoji_and_escapes),
        ("int64 extremes and overflow literal", int64_extremes),
        ("unsupported forms must error, state unchanged", unsupported_forms_error),
        ("nested BEGIN rejected, stray COMMIT harmless", nested_transaction_and_stray_control),
        ("string order is code point order", code_point_ordering),
        ("SELECT * includes the vector as an array", select_star_with_vector),
        ("empty results, LIMIT 0, oversized LIMIT, no-match writes", empty_queries_and_limits),
        ("ORDER BY alias, multi key, DESC on distance", alias_ordering_and_multi_key),
        ("VECTOR(1) distances", vector_dimension_one),
        ("VECTOR(128) distances", vector_128_dims),
        ("TopK traps: ties, filter smaller than K, empty filter", topk_traps),
        ("ann equals exact on ties and filters", ann_matches_exact_on_ties),
        ("transaction sees its own writes", transaction_sees_own_changes),
        ("error inside transaction then commit", error_then_continue_in_transaction),
        ("committed survives kill and repeated recovery kills", commit_survives_kill_and_recovery_kill),
        ("protocol: id types, unknown op, bad json", protocol_id_types_and_errors),
        ("shutdown is silent with exit code 0", shutdown_is_silent_and_zero),
        ("stdin EOF exits 0", stdin_eof_exits_zero),
        ("2000 rows then restart keeps data and TopK", bulk_then_restart),
        ("index maintenance interleaved with DML", index_maintenance_interleaved),
        ("CREATE TABLE and index edge cases", create_table_edges),
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
