#!/usr/bin/env python3
"""Transaction matrix probe (JSON Lines protocol).

Spec 2.3: statements inside an explicit transaction see the transaction's own completed changes -
including vector search - a failed statement leaves no partial change while earlier statements can
still be committed or rolled back, and a rollback restores the record contents, the visible row set
and the vector search results to the pre-transaction state.

The expected TopK order is computed from a model of the stored vectors instead of being written out
by hand: distances are computed from the float32 values, and ties break on the primary key (spec 2.2).
"""
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
PROBLEMS = []
CHECKS = [0]
Q = "[1,0,0,0]"
MODEL = {}  # id -> float32 vector, the committed state the probe expects


def f32(x):
    return struct.unpack("f", struct.pack("f", x))[0]


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
                raise RuntimeError("engine closed its output")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql):
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": "exact"}) + "\n").encode())
        return json.loads(self.line())

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()

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


def ids(reply):
    if not reply.get("ok"):
        return None
    cols = reply.get("columns") or []
    if "id" not in cols:
        return None
    i = cols.index("id")
    return [row[i] for row in (reply.get("rows") or [])]


def pairs(reply):
    if not reply.get("ok"):
        return None
    cols = reply.get("columns") or []
    if "id" not in cols:
        return None
    i = cols.index("id")
    others = [n for n, c in enumerate(cols) if c != "id"]
    out = []
    for row in (reply.get("rows") or []):
        d = row[others[0]] if others else None
        out.append((row[i], None if d is None else float(d)))
    return out


def expected(state, k):
    scored = []
    q = [f32(float(x)) for x in Q.strip("[]").split(",")]
    for pk, vec in state.items():
        total = 0.0
        for a, b in zip(vec, q):
            d = a - b
            total += d * d
        scored.append((total, pk))
    scored.sort()
    return [(pk, total ** 0.5) for total, pk in scored[:k]]


def order_matches(label, reply, state, k=3):
    got = pairs(reply)
    want = expected(state, k)
    ok = got is not None and [p[0] for p in got] == [p[0] for p in want] and \
        all(abs((g[1] or 0) - w[1]) < 1e-5 for g, w in zip(got, want))
    check(label, ok, "want %r got %r" % (want, got))
    return ok


def main():
    global MODEL
    tmp = tempfile.mkdtemp(prefix="mvdb_tx_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        s = S(data)
        s.req("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(16), emb VECTOR(4))")
        s.req("INSERT INTO docs VALUES (1,'a','[1,0,0,0]'),(2,'a','[0,1,0,0]'),(3,'b','[0,0,1,0]')")
        MODEL = {1: [1.0, 0.0, 0.0, 0.0], 2: [0.0, 1.0, 0.0, 0.0], 3: [0.0, 0.0, 1.0, 0.0]}
        top = lambda: s.req("SELECT id, L2_DISTANCE(emb,%s) AS d FROM docs ORDER BY d LIMIT 3" % Q)
        order_matches("seed topk", top(), MODEL)
        check("seed order is 1,2,3", [p[0] for p in pairs(top())] == [1, 2, 3], str(pairs(top())))

        # --- 1. vector search inside a transaction sees its own vector update
        s.req("BEGIN")
        r = s.req("UPDATE docs SET emb='[0.9,0,0,0]' WHERE id=3")
        check("update inside transaction", r.get("ok") is True and r.get("affected_rows") == 1,
              json.dumps(r)[:160])
        inside_model = dict(MODEL)
        inside_model[3] = [f32(0.9), 0.0, 0.0, 0.0]
        order_matches("transaction sees its own vector update", top(), inside_model)
        vec = (s.req("SELECT emb FROM docs WHERE id=3").get("rows") or [[None]])[0][0]
        check("transaction reads the updated vector",
              isinstance(vec, list) and abs(float(vec[0]) - f32(0.9)) < 1e-6, str(vec))

        # --- 2. rollback restores the vector search result
        r = s.req("ROLLBACK")
        check("rollback ok", r.get("ok") is True, json.dumps(r)[:160])
        order_matches("rollback restores the topk order and distances", top(), MODEL)

        # --- 3. a failed statement inside a transaction leaves nothing behind, earlier work commits
        s.req("BEGIN")
        s.req("UPDATE docs SET emb='[0.9,0,0,0]' WHERE id=3")
        dup = s.req("INSERT INTO docs VALUES (1,'dup','[1,1,1,1]')")
        check("duplicate inside transaction fails", dup.get("ok") is False, json.dumps(dup)[:160])
        bad = s.req("INSERT INTO docs VALUES (4,'c','[1]')")
        check("bad dimension inside transaction fails", bad.get("ok") is False, json.dumps(bad)[:160])
        multi = s.req("INSERT INTO docs VALUES (4,'c','[1,1,1,1]'),(1,'dup','[1,1,1,1]')")
        check("multi-row insert with a duplicate fails", multi.get("ok") is False,
              json.dumps(multi)[:160])
        check("failed inserts left no row", ids(s.req("SELECT id FROM docs WHERE id=4")) == [],
              str(ids(s.req("SELECT id FROM docs WHERE id=4"))))
        order_matches("earlier update still visible after the failures", top(), inside_model)
        r = s.req("COMMIT")
        check("commit after a failed statement", r.get("ok") is True, json.dumps(r)[:160])
        MODEL[3] = [f32(0.9), 0.0, 0.0, 0.0]
        order_matches("committed update survived", top(), MODEL)

        # --- 4. failure then rollback leaves nothing at all
        s.req("BEGIN")
        r = s.req("UPDATE docs SET emb='[0,0,0,1]' WHERE id=2")
        check("update before rollback", r.get("ok") is True, json.dumps(r)[:160])
        s.req("INSERT INTO docs VALUES (2,'dup','[0,0,0,0]')")
        r = s.req("ROLLBACK")
        check("rollback after a failed statement", r.get("ok") is True, json.dumps(r)[:160])
        vec = (s.req("SELECT emb FROM docs WHERE id=2").get("rows") or [[None]])[0][0]
        check("rolled back update did not stick",
              isinstance(vec, list) and abs(float(vec[0])) < 1e-6, str(vec))
        order_matches("topk after the rolled back update", top(), MODEL)

        # --- 5. delete then rollback: the row returns to the search results
        s.req("BEGIN")
        s.req("DELETE FROM docs WHERE id=3")
        check("delete visible inside the transaction", ids(s.req("SELECT id FROM docs")) == [1, 2],
              str(ids(s.req("SELECT id FROM docs"))))
        gone_model = {k: v for k, v in MODEL.items() if k != 3}
        order_matches("topk inside the transaction without the deleted row", top(), gone_model)
        s.req("ROLLBACK")
        check("rollback restores the deleted row", ids(s.req("SELECT id FROM docs")) == [1, 2, 3],
              str(ids(s.req("SELECT id FROM docs"))))
        order_matches("topk after the rolled back delete", top(), MODEL)

        # --- 6. DDL inside a transaction must not leave a half-created table behind
        s.req("BEGIN")
        r = s.req("CREATE TABLE t2 (id BIGINT PRIMARY KEY, v VECTOR(2))")
        check("create table inside a transaction", r.get("ok") is True, json.dumps(r)[:160])
        if r.get("ok"):
            s.req("INSERT INTO t2 VALUES (1,'[0,0]')")
            r = s.req("ROLLBACK")
            check("rollback of a transaction containing DDL", r.get("ok") is True, json.dumps(r)[:160])
            probe = s.req("SELECT id FROM t2")
            rows_left = ids(probe) if probe.get("ok") else None
            # Either the table is gone (DDL rolled back) or it exists empty; a table that still holds
            # the rolled back row would mean the catalog and the data disagree.
            check("no half-applied DDL after rollback", rows_left is None or rows_left == [],
                  str(probe)[:200])

        # --- 7. COMMIT / ROLLBACK without a transaction must not crash or corrupt anything
        r1 = s.req("COMMIT")
        r2 = s.req("ROLLBACK")
        check("commit/rollback outside a transaction answer safely",
              (r1.get("ok") in (True, False)) and (r2.get("ok") in (True, False)),
              json.dumps(r1)[:80] + json.dumps(r2)[:80])
        check("table intact after stray commit/rollback", ids(s.req("SELECT id FROM docs")) == [1, 2, 3],
              str(ids(s.req("SELECT id FROM docs"))))

        # --- 8. a nested BEGIN error must not damage the open transaction
        s.req("BEGIN")
        s.req("UPDATE docs SET category='nested' WHERE id=1")
        r = s.req("BEGIN")
        check("nested begin rejected", r.get("ok") is False, json.dumps(r)[:160])
        r = s.req("COMMIT")
        check("outer transaction still commits", r.get("ok") is True, json.dumps(r)[:160])
        cat = (s.req("SELECT category FROM docs WHERE id=1").get("rows") or [[None]])[0][0]
        check("outer transaction write applied", cat == "nested", str(cat))

        # --- 9. committed data survives a hard kill; an open transaction does not
        s.req("BEGIN")
        s.req("INSERT INTO docs VALUES (9,'open','[0.5,0.5,0,0]')")
        s.req("UPDATE docs SET category='open' WHERE id=2")
        s.req("UPDATE docs SET emb='[0,0,0,1]' WHERE id=2")
        s.kill()
        s = S(data)
        check("kill: open transaction left nothing", ids(s.req("SELECT id FROM docs")) == [1, 2, 3],
              str(ids(s.req("SELECT id FROM docs"))))
        cat = (s.req("SELECT category FROM docs WHERE id=2").get("rows") or [[None]])[0][0]
        check("kill: open update did not stick", cat == "a", str(cat))
        order_matches("kill: vector search consistent after recovery", top(), MODEL)

        # --- 10. graceful shutdown with an open transaction rolls it back
        s.req("BEGIN")
        s.req("INSERT INTO docs VALUES (10,'bye','[0,0,0,1]')")
        s.req("UPDATE docs SET emb='[0,0,0,1]' WHERE id=1")
        s.close()
        s = S(data)
        check("shutdown rolled the open transaction back", ids(s.req("SELECT id FROM docs")) == [1, 2, 3],
              str(ids(s.req("SELECT id FROM docs"))))
        order_matches("shutdown: vector search consistent after restart", top(), MODEL)
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
