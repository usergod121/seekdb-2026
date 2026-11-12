#!/usr/bin/env python3
"""Predicate types on the filtered-TopK path.

The fast path compiles a WHERE into a reader over the columnar cache. That reader only exists for the
type it was built for and applies no coercion, so a comparison it cannot express must go through the
general evaluator - otherwise it reads an array that is empty for that column and the process dies.

Every shape below used to either crash the process (quoted integer against a BIGINT column, any
comparison against a vector column) or silently match nothing (a number against a VARCHAR column) when
combined with ORDER BY L2_DISTANCE(...) LIMIT n. The rule now is that the two paths must agree.
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

    def req(self, sql):
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": "exact"}) + "\n").encode())
        raw = self.line()
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return {"ok": False, "error": {"code": "BAD_JSON", "message": raw[:120]}}

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


def ids(reply):
    if reply is None or not reply.get("ok"):
        return None
    cols = reply.get("columns") or []
    if "id" not in cols:
        return None
    i = cols.index("id")
    return [row[i] for row in (reply.get("rows") or [])]


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_predtypes_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        s = S(data)
        s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, s VARCHAR(8), emb VECTOR(2))")
        s.req("INSERT INTO t VALUES (1,'1','[1,0]'),(2,'2','[0,1]'),(3,'3','[1,1]'),(4,'x','[2,2]')")

        # every comparison, in both the general path and the filtered-TopK fast path
        shapes = [
            ("quoted integer on a BIGINT column", "id = '2'"),
            ("quoted integer range", "id >= '2'"),
            ("number against a VARCHAR column", "s = 2"),
            ("number range against a VARCHAR column", "s >= 2"),
            ("text against a VARCHAR column", "s = '2'"),
            ("plain integer", "id > 1"),
            ("two conjuncts of mixed types", "id >= 2 AND s <= '3'"),
            ("conjunct with a vector comparison", "id > 0 AND emb <= '[1,1]'"),
        ]
        for label, predicate in shapes:
            general = s.req("SELECT id FROM t WHERE %s ORDER BY id" % predicate)
            fast = s.req("SELECT id FROM t WHERE %s ORDER BY L2_DISTANCE(emb,'[1,0]') LIMIT 9"
                         % predicate)
            check(label + " answers on the general path", general is not None,
                  "process gone (general path)")
            check(label + " answers on the fast path", fast is not None,
                  "process gone (fast path)")
            if general is None or fast is None:
                return 1
            left = ids(general) if general.get("ok") else None
            right = sorted(ids(fast)) if fast.get("ok") else None
            if left is None and right is None:
                check(label + " consistent (both reject)", True)
            else:
                check(label + " consistent between the two paths",
                      left is not None and right is not None and sorted(left) == right,
                      "general=%r fast=%r" % (left, right))

        # the vector comparison itself must be rejected, not answered with garbage
        for predicate in ["emb = '[1,0]'", "emb < '[0.5,0.5]'", "emb >= '[0.5,0.5]'",
                          "emb != '[1,0]'"]:
            reply = s.req("SELECT id FROM t WHERE %s" % predicate)
            check("vector comparison %r is rejected" % predicate,
                  reply is not None and reply.get("ok") is False,
                  json.dumps(reply)[:120] if reply else "process gone")
            reply = s.req("SELECT id FROM t WHERE %s ORDER BY L2_DISTANCE(emb,'[1,0]') LIMIT 2"
                          % predicate)
            check("vector comparison %r in a TopK is rejected" % predicate,
                  reply is not None and reply.get("ok") is False,
                  json.dumps(reply)[:120] if reply else "process gone")

        # and the engine is still usable afterwards
        check("engine still answers after the rejects", ids(s.req("SELECT id FROM t ORDER BY id"))
              == [1, 2, 3, 4])
        check("TopK still correct", ids(s.req(
            "SELECT id FROM t ORDER BY L2_DISTANCE(emb,'[1,0]') LIMIT 2")) == [1, 3])
        s.close()
        s = S(data)
        check("quoted integer still works after a restart",
              ids(s.req("SELECT id FROM t WHERE id = '2'")) == [2])
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
