#!/usr/bin/env python3
"""Adversarial JSON-protocol checks that a formal case could plausibly contain.

Talks the published JSON-lines protocol (not the old text protocol) and, when --image is given, runs
every session inside the judge's own container shape: --network=none --read-only --cap-drop=ALL with a
bind mount for /data. Each scenario prints PASS/FAIL so a failure names the behaviour that is missing.
"""
import argparse
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time

DIMS = 4


class Session:
    def __init__(self, argv, env=None):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0, env=env)
        self.buf = b""
        self.seq = 0
        first = self.line()
        if json.loads(first) != {"type": "ready"}:
            raise RuntimeError("bad handshake: %r" % first)

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("engine closed its output")
            self.buf += chunk
        out, _, self.buf = self.buf.partition(b"\n")
        return out.decode("utf-8", "replace").rstrip("\r")

    def request(self, sql=None, op="execute", mode="exact"):
        self.seq += 1
        msg = {"id": self.seq, "op": op}
        if sql is not None:
            msg["sql"] = sql
            msg["search_mode"] = mode
        os.write(self.proc.stdin.fileno(), (json.dumps(msg, ensure_ascii=False) + "\n").encode())
        reply = json.loads(self.line())
        if reply.get("id") != self.seq:
            raise RuntimeError("id mismatch: %r" % reply)
        if not isinstance(reply.get("ok"), bool):
            raise RuntimeError("ok is not a boolean: %r" % reply)
        return reply

    def shutdown(self):
        reply = self.request(op="shutdown")
        if reply.get("ok") is not True:
            raise RuntimeError("shutdown not ok: %r" % reply)
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        code = self.proc.wait(timeout=30)
        extra = self.buf + self.proc.stdout.read()
        return code, extra

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()


def make_argv(args, data_dir):
    if args.image:
        return ["docker", "run", "--rm", "-i", "--network=none", "--read-only", "--cap-drop=ALL",
                "--cpus=2", "--memory=2g", "--pids-limit=128",
                "-v", "%s:/data" % data_dir, args.image, "--data-dir", "/data"]
    return [args.binary, "--data-dir", data_dir]


def scenario(name, results, body):
    try:
        ok, detail = body()
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, "exception: %s" % exc
    results.append((name, ok, detail))
    print("%-52s %s%s" % (name, "PASS" if ok else "FAIL", "" if ok else "  <- " + detail))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=None)
    ap.add_argument("--binary", default="/mnt/e/mvdb/build/minivectordb")
    args = ap.parse_args()
    results = []

    def fresh():
        tmp = tempfile.mkdtemp(prefix="mvdb_adv_")
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        os.chmod(data, 0o777)
        return tmp, data

    # 1. restart the process (equivalent to the judge restarting the container) keeps committed data
    def restart_persistence():
        tmp, data = fresh()
        try:
            s = Session(make_argv(args, data))
            s.request("CREATE TABLE docs (id BIGINT PRIMARY KEY, content VARCHAR(16));")
            s.request("INSERT INTO docs VALUES (1, 'kept');")
            code, extra = s.shutdown()
            if code != 0 or extra:
                return False, "shutdown exit=%r extra=%r" % (code, extra[:80])
            s = Session(make_argv(args, data))
            reply = s.request("SELECT id, content FROM docs;")
            s.shutdown()
            return reply.get("rows") == [[1, "kept"]], "rows=%r" % (reply.get("rows"),)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # 2. hard kill mid-transaction: committed survives, open transaction leaves nothing
    def kill_mid_transaction():
        tmp, data = fresh()
        try:
            s = Session(make_argv(args, data))
            s.request("CREATE TABLE docs (id BIGINT PRIMARY KEY, content VARCHAR(16));")
            s.request("INSERT INTO docs VALUES (1, 'committed');")
            s.request("BEGIN;")
            s.request("INSERT INTO docs VALUES (2, 'open');")
            s.kill()
            s = Session(make_argv(args, data))
            reply = s.request("SELECT id FROM docs ORDER BY id;")
            s.shutdown()
            return reply.get("rows") == [[1]], "rows=%r" % (reply.get("rows"),)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # 3. kill during recovery, three times, then the committed state must still be complete
    def kill_during_recovery():
        tmp, data = fresh()
        try:
            s = Session(make_argv(args, data))
            s.request("CREATE TABLE docs (id BIGINT PRIMARY KEY, v VECTOR(%d));" % DIMS)
            for batch in range(12):
                s.request("BEGIN;")
                values = ",".join("(%d,'[%s]')" % (batch * 20 + i + 1,
                                                   ",".join("%.3f" % (i * 0.01) for _ in range(DIMS)))
                                  for i in range(20))
                s.request("INSERT INTO docs VALUES " + values + ";")
                s.request("COMMIT;")
            s.kill()
            for _ in range(3):
                proc = subprocess.Popen(make_argv(args, data), stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(0.05)
                proc.send_signal(signal.SIGKILL)
                proc.wait()
            s = Session(make_argv(args, data))
            reply = s.request("SELECT id FROM docs;")
            s.shutdown()
            rows = reply.get("rows") or []
            ids = sorted(int(r[0]) for r in rows)
            return len(ids) == 240 and ids == list(range(1, 241)), "%d rows" % len(ids)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # 4. invalid vectors are rejected and leave nothing behind
    def invalid_vectors():
        tmp, data = fresh()
        try:
            s = Session(make_argv(args, data))
            s.request("CREATE TABLE docs (id BIGINT PRIMARY KEY, v VECTOR(2));")
            bad = ["'[nan,0]'", "'[Infinity,0]'", "'[-inf,0]'", "'[1e40,0]'", "'[3.5e38,0]'",
                   "'[1,2,3]'", "'[1]'", "'oops'"]
            for literal in bad:
                reply = s.request("INSERT INTO docs VALUES (1, %s);" % literal)
                if reply.get("ok") is not False:
                    return False, "accepted %s" % literal
                code = (reply.get("error") or {}).get("code")
                if not isinstance(code, str) or not code:
                    return False, "empty error code for %s" % literal
            reply = s.request("SELECT id FROM docs;")
            s.shutdown()
            return reply.get("rows") == [], "rows=%r" % (reply.get("rows"),)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # 5. a multi-row insert with one bad row stores nothing
    def statement_atomicity():
        tmp, data = fresh()
        try:
            s = Session(make_argv(args, data))
            s.request("CREATE TABLE docs (id BIGINT PRIMARY KEY, v VECTOR(2));")
            s.request("INSERT INTO docs VALUES (1,'[0,0]');")
            reply = s.request("INSERT INTO docs VALUES (2,'[1,1]'),(3,'[1]');")
            if reply.get("ok") is not False:
                return False, "bad insert accepted"
            reply = s.request("SELECT id FROM docs;")
            s.shutdown()
            return reply.get("rows") == [[1]], "rows=%r" % (reply.get("rows"),)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # 6. bigint precision and float32 distance semantics
    def bigint_and_float32():
        tmp, data = fresh()
        try:
            s = Session(make_argv(args, data))
            s.request("CREATE TABLE big (id BIGINT PRIMARY KEY, v BIGINT);")
            reply = s.request("INSERT INTO big VALUES (9007199254740993, -9007199254740993);")
            if reply.get("ok") is not True:
                return False, "bigint insert rejected"
            reply = s.request("SELECT id, v FROM big;")
            if reply.get("rows") != [[9007199254740993, -9007199254740993]]:
                return False, "bigint rows=%r" % (reply.get("rows"),)
            s.request("CREATE TABLE docs (id BIGINT PRIMARY KEY, v VECTOR(2));")
            s.request("INSERT INTO docs VALUES (1,'[16777217,0]'),(2,'[16777216,0]');")
            reply = s.request(
                "SELECT id, L2_DISTANCE(v,'[16777216,0]') AS d FROM docs ORDER BY d LIMIT 2;")
            s.shutdown()
            rows = reply.get("rows") or []
            ok = len(rows) == 2 and all(abs(float(r[1])) <= 1e-6 for r in rows)
            return ok, "rows=%r" % (rows,)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # 7. ties break on ascending primary key, and a filter applies before TopK
    def ties_and_filter():
        tmp, data = fresh()
        try:
            s = Session(make_argv(args, data))
            s.request("CREATE TABLE docs (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(2));")
            s.request("INSERT INTO docs VALUES (8,'keep','[1,0]'),(3,'keep','[-1,0]'),"
                      "(1,'skip','[0,0]'),(2,'skip','[0,0]');")
            reply = s.request("SELECT id, L2_DISTANCE(v,'[0,0]') AS d FROM docs "
                              "WHERE cat = 'keep' ORDER BY d LIMIT 9;")
            s.shutdown()
            return reply.get("rows") == [[3, 1], [8, 1]], "rows=%r" % (reply.get("rows"),)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # 8. ann mode must answer the same as exact for a small data set
    def ann_equals_exact():
        tmp, data = fresh()
        try:
            s = Session(make_argv(args, data))
            s.request("CREATE TABLE docs (id BIGINT PRIMARY KEY, v VECTOR(2));")
            s.request("INSERT INTO docs VALUES (1,'[0,0]'),(2,'[3,4]'),(3,'[1,0]');")
            s.request("CREATE VECTOR INDEX idx ON docs (v);")
            exact = s.request("SELECT id, L2_DISTANCE(v,'[0,0]') AS d FROM docs ORDER BY d LIMIT 3;")
            ann = s.request("SELECT id, L2_DISTANCE(v,'[0,0]') AS d FROM docs ORDER BY d LIMIT 3;",
                            mode="ann")
            s.shutdown()
            return exact.get("rows") == ann.get("rows") == [[1, 0], [3, 1], [2, 5]], \
                "exact=%r ann=%r" % (exact.get("rows"), ann.get("rows"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # 9. empty result still returns column names; LIMIT 0 and oversized LIMIT behave
    def empty_and_limits():
        tmp, data = fresh()
        try:
            s = Session(make_argv(args, data))
            s.request("CREATE TABLE docs (id BIGINT PRIMARY KEY);")
            reply = s.request("SELECT id FROM docs;")
            if reply.get("columns") != ["id"] or reply.get("rows") != []:
                return False, "empty select=%r" % (reply,)
            s.request("INSERT INTO docs VALUES (1),(2),(3);")
            if s.request("SELECT id FROM docs LIMIT 0;").get("rows") != []:
                return False, "LIMIT 0 returned rows"
            if len(s.request("SELECT id FROM docs LIMIT 99;").get("rows") or []) != 3:
                return False, "oversized LIMIT wrong"
            s.shutdown()
            return True, ""
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # 10. data larger than the row-cache budget still answers exactly
    def over_budget():
        tmp, data = fresh()
        env = dict(os.environ, MVDB_CACHE_BUDGET_BYTES="65536")
        try:
            argv = make_argv(args, data)
            if args.image:
                argv = [a for a in argv if a != "-i"] + []
                argv = argv[:1] + ["run", "--rm", "-i", "--network=none", "--cap-drop=ALL",
                                   "-e", "MVDB_CACHE_BUDGET_BYTES=65536",
                                   "-v", "%s:/data" % data, args.image, "--data-dir", "/data"]
            s = Session(argv, env=env if not args.image else None)
            s.request("CREATE TABLE docs (id BIGINT PRIMARY KEY, v VECTOR(2));")
            for start in range(0, 300, 50):
                values = ",".join("(%d,'[%d,%d]')" % (i + 1, i % 7, i % 5)
                                  for i in range(start, start + 50))
                s.request("INSERT INTO docs VALUES " + values + ";")
            reply = s.request("SELECT id, L2_DISTANCE(v,'[0,0]') AS d FROM docs "
                              "ORDER BY d LIMIT 3;")
            s.shutdown()
            rows = reply.get("rows") or []
            ties = [[1, 0], [36, 0], [71, 0]]   # several points sit exactly on the query point
            return rows == ties, "rows=%r" % (rows[:3],)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    for name, body in [
        ("restart keeps committed data", restart_persistence),
        ("hard kill keeps committed, drops open tx", kill_mid_transaction),
        ("killed during recovery still recovers", kill_during_recovery),
        ("invalid vectors rejected, nothing stored", invalid_vectors),
        ("multi-row insert is atomic", statement_atomicity),
        ("bigint precision and float32 distance", bigint_and_float32),
        ("ties by primary key, filter before TopK", ties_and_filter),
        ("ann answers identical to exact", ann_equals_exact),
        ("empty result and LIMIT edges", empty_and_limits),
        ("data beyond the cache budget still exact", over_budget),
    ]:
        scenario(name, results, body)

    failed = [name for name, ok, _ in results if not ok]
    print()
    print("%d/%d scenario(s) passed" % (len(results) - len(failed), len(results)))
    for name in failed:
        print("  FAILED: " + name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
