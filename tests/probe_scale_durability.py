#!/usr/bin/env python3
"""Scale and durability probe (JSON Lines protocol).

Spec 2.3 says the formal tests include data sets larger than the memory limit, so this probe keeps an
exact model of the table in Python and checks the engine against it after every step: batched inserts
in explicit transactions, updates and deletes, a rollback, a clean restart, a hard kill and a restart
under a 1 MiB cache budget where the whole data set no longer fits.
"""
import json
import os
import random
import shutil
import signal
import struct
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
DIMS = 64
PROBLEMS = []
CHECKS = [0]


def f32(x):
    return struct.unpack("f", struct.pack("f", x))[0]


class Session:
    def __init__(self, data_dir, env=None):
        self.proc = subprocess.Popen([BIN, "--data-dir", data_dir], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                                     env=env)
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

    def req(self, sql, mode="exact"):
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": mode}) + "\n").encode())
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
            self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()


def check(label, condition, detail=""):
    CHECKS[0] += 1
    if not condition:
        PROBLEMS.append(label + ("  " + detail if detail else ""))
        print("FAIL  " + label + ("  " + detail if detail else ""))
    else:
        print("ok    " + label)


def ids_of(reply):
    if not reply.get("ok"):
        return None
    cols = reply.get("columns") or []
    if "id" not in cols:
        return None
    i = cols.index("id")
    return [row[i] for row in (reply.get("rows") or [])]


def exact_topk(model, qvec, k, category=None):
    scored = []
    for pk, (cat, vec) in model.items():
        if category is not None and cat != category:
            continue
        total = 0.0
        for a, b in zip(vec, qvec):
            d = a - b
            total += d * d
        scored.append((total, pk))
    scored.sort()
    return [pk for _, pk in scored[:k]]


def main():
    rows = 20000
    rng = random.Random(20261111)
    tmp = tempfile.mkdtemp(prefix="mvdb_scale_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    model = {}
    literals = {}
    try:
        s = Session(data)
        r = s.req("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), "
                  "title VARCHAR(32), emb VECTOR(%d))" % DIMS)
        check("create table", r.get("ok") is True, json.dumps(r)[:200])

        # Batched inserts inside explicit transactions; every 500th vector repeats so the primary key
        # tie-break is exercised too.
        batch = 500
        for start in range(1, rows + 1, batch):
            parts = []
            s.req("BEGIN")
            for i in range(start, min(start + batch, rows + 1)):
                if i % 500 == 0 and i > 1:
                    vec = literals[i - 1]
                else:
                    vec = [round(rng.uniform(-1, 1), 4) for _ in range(DIMS)]
                literals[i] = vec
                cat = "c%d" % (i % 8)
                model[i] = (cat, [f32(v) for v in vec])
                parts.append("(%d,'%s','doc %d','[%s]')" % (
                    i, cat, i, ",".join("%.4f" % v for v in vec)))
            r = s.req("INSERT INTO docs VALUES " + ",".join(parts))
            if not (r.get("ok") is True and r.get("affected_rows") == len(parts)):
                check("batch insert %d" % start, False, json.dumps(r)[:200])
                break
            r = s.req("COMMIT")
            if r.get("ok") is not True:
                check("commit %d" % start, False, json.dumps(r)[:200])
                break
        check("inserted %d rows" % rows, len(model) == rows, "%d" % len(model))

        def compare_topk(session, label, budget_note=""):
            bad = 0
            for probe in range(6):
                qvec = [round(rng.uniform(-1, 1), 4) for _ in range(DIMS)]
                literal = ",".join("%.4f" % v for v in qvec)
                want = exact_topk(model, [f32(v) for v in qvec], 10)
                got = ids_of(session.req(
                    "SELECT id FROM docs ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT 10" % literal))
                if got != want:
                    bad += 1
                    if bad == 1:
                        print("      unfiltered mismatch: want %r got %r" % (want[:4], (got or [])[:4]))
                cat = "c%d" % (probe % 8)
                want_f = exact_topk(model, [f32(v) for v in qvec], 10, cat)
                got_f = ids_of(session.req(
                    "SELECT id FROM docs WHERE category='%s' "
                    "ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT 10" % (cat, literal)))
                if got_f != want_f:
                    bad += 1
                    if bad == 1:
                        print("      filtered mismatch: want %r got %r" % (want_f[:4], (got_f or [])[:4]))
            check("TopK matches the exact answer " + label + budget_note, bad == 0, "%d wrong" % bad)

        check("row count after inserts",
              len(ids_of(s.req("SELECT id FROM docs")) or []) == rows)
        compare_topk(s, "after inserts")

        # --- mutations through autocommit, then a rolled back transaction
        for i in range(1, 201):
            r = s.req("UPDATE docs SET category='upd' WHERE id=%d" % i)
            if r.get("ok") is not True or r.get("affected_rows") != 1:
                check("update %d" % i, False, json.dumps(r)[:160])
                break
            model[i] = ("upd", model[i][1])
        for i in range(300, 401):
            r = s.req("DELETE FROM docs WHERE id=%d" % i)
            if r.get("ok") is not True or r.get("affected_rows") != 1:
                check("delete %d" % i, False, json.dumps(r)[:160])
                break
            del model[i]
        check("mutation count", len(ids_of(s.req("SELECT id FROM docs")) or []) == len(model),
              "%d vs %d" % (len(ids_of(s.req("SELECT id FROM docs")) or []), len(model)))
        compare_topk(s, "after updates and deletes")

        # --- a transaction that mixes updates and deletes and then rolls back
        snapshot = {k: v for k, v in model.items()}
        s.req("BEGIN")
        s.req("UPDATE docs SET category='tx' WHERE id<=50")
        s.req("DELETE FROM docs WHERE id>%d" % (rows - 100))
        mid = ids_of(s.req("SELECT id FROM docs WHERE category='tx'"))
        check("transaction sees its own update", mid is not None and len(mid) == 50, str((mid or [])[:3]))
        r = s.req("ROLLBACK")
        check("rollback ok", r.get("ok") is True, json.dumps(r)[:160])
        after = ids_of(s.req("SELECT id FROM docs"))
        check("rollback restored the row set", after is not None and sorted(after) == sorted(model),
              "%d vs %d" % (len(after or []), len(model)))
        compare_topk(s, "after rollback")
        model = snapshot

        # --- clean restart
        s.close()
        s = Session(data)
        after = ids_of(s.req("SELECT id FROM docs"))
        check("clean restart keeps every row", after is not None and sorted(after) == sorted(model),
              "%d vs %d" % (len(after or []), len(model)))
        compare_topk(s, "after clean restart")

        # --- hard kill after committed work, then restart
        s.req("BEGIN")
        s.req("INSERT INTO docs VALUES (%d,'c0','post','[%s]')" % (
            rows + 1, ",".join("0.0000" for _ in range(DIMS))))
        s.kill()
        s = Session(data)
        after = ids_of(s.req("SELECT id FROM docs"))
        check("kill: committed rows survive, open transaction does not",
              after is not None and sorted(after) == sorted(model),
              "%d vs %d" % (len(after or []), len(model)))
        compare_topk(s, "after hard kill")
        s.close()

        # --- the same data set with a 1 MiB page cache: every query must page from disk
        env = dict(os.environ, MVDB_CACHE_BUDGET_BYTES="1048576")
        s = Session(data, env=env)
        after = ids_of(s.req("SELECT id FROM docs"))
        check("over-budget restart keeps every row", after is not None and sorted(after) == sorted(model),
              "%d vs %d" % (len(after or []), len(model)))
        compare_topk(s, "over budget", " (1 MiB cache)")
        r = s.req("UPDATE docs SET category='of' WHERE id<=30")
        check("over-budget update", r.get("ok") is True and r.get("affected_rows") == 30,
              json.dumps(r)[:160])
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
