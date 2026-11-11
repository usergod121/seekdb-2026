#!/usr/bin/env python3
"""Behaviour snapshot for edge constructs whose semantics the spec leaves implicit.

Each case is executed against a live engine and the reply is printed, so the behaviour can be
compared against MySQL semantics by eye. Cases marked SPEC are ones the task specification or the
public case set settle explicitly; the rest are places where a formal case could disagree.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")


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
        try:
            return json.loads(raw)
        except Exception:
            return {"ok": False, "error": {"code": "BAD_JSON", "message": raw}}

    def close(self):
        try:
            self.seq += 1
            os.write(self.proc.stdin.fileno(),
                     (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
            self.line()
            self.proc.wait(timeout=20)
        except Exception:
            self.proc.kill()


def show(label, reply, note=""):
    if reply.get("ok"):
        if "columns" in reply:
            body = "rows=%s" % json.dumps(reply.get("rows"), ensure_ascii=False)[:150]
        else:
            body = "affected_rows=%s" % reply.get("affected_rows")
        line = "ok    %-42s %s" % (label, body)
    else:
        err = reply.get("error") or {}
        line = "err   %-42s code=%s msg=%s" % (label, err.get("code"), str(err.get("message"))[:60])
    print(line + (("   <- " + note) if note else ""))


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_edges_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        s = S(data)
        s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, note VARCHAR(16), emb VECTOR(2))")
        s.req("INSERT INTO t VALUES (1,'a','[1,0]'),(2,'b','[0,1]'),(3,'c','[1,1]'),(4,'a','[2,2]')")

        print("== affected_rows semantics")
        show("update to the same value", s.req("UPDATE t SET note='a' WHERE id=1"))
        show("update no match", s.req("UPDATE t SET note='z' WHERE id=999"))
        show("delete no match", s.req("DELETE FROM t WHERE id=999"))
        show("update many rows", s.req("UPDATE t SET note='a' WHERE note='a'"))
        show("delete many rows", s.req("DELETE FROM t WHERE id=4"))
        show("restore row 4", s.req("INSERT INTO t VALUES (4,'a','[2,2]')"))

        print("== primary key updates")
        show("update pk to a free key", s.req("UPDATE t SET id=50 WHERE id=4"))
        r = s.req("SELECT id FROM t ORDER BY id")
        print("      ids now:", json.dumps(r.get("rows")))
        show("select the new key", s.req("SELECT note FROM t WHERE id=50"))
        show("select the old key", s.req("SELECT note FROM t WHERE id=4"))
        s.req("UPDATE t SET id=4 WHERE id=50")

        print("== literal coercion")
        show("explicit plus sign", s.req("INSERT INTO t VALUES (+5,'p','[0,0]')"))
        show("scientific into BIGINT", s.req("INSERT INTO t VALUES (1e3,'q','[0,0]')"))
        show("float into BIGINT", s.req("INSERT INTO t VALUES (6.7,'r','[0,0]')"))
        show("string into BIGINT", s.req("INSERT INTO t VALUES ('7','s','[0,0]')"))
        show("int into VARCHAR", s.req("INSERT INTO t VALUES (8,42,'[0,0]')"))
        show("vector into VARCHAR", s.req("INSERT INTO t VALUES (9,[0,0],'[0,0]')"))
        show("null into BIGINT", s.req("INSERT INTO t VALUES (NULL,'n','[0,0]')"))
        show("null into VARCHAR", s.req("INSERT INTO t VALUES (10,NULL,'[0,0]')"))
        show("set column to NULL", s.req("UPDATE t SET note=NULL WHERE id=1"))
        show("is null predicate", s.req("SELECT id FROM t WHERE note IS NULL"))

        print("== statement shapes")
        show("limit with offset", s.req("SELECT id FROM t LIMIT 1,2"))
        show("limit negative", s.req("SELECT id FROM t LIMIT -1"))
        show("limit huge", s.req("SELECT id FROM t LIMIT 999999999999999999999"))
        show("order by ordinal", s.req("SELECT id FROM t ORDER BY 1"))
        show("backtick table name", s.req("SELECT id FROM `t`"))
        show("uppercase identifiers", s.req("SELECT ID FROM T"))
        show("count star", s.req("SELECT COUNT(*) FROM t"))
        show("like predicate", s.req("SELECT id FROM t WHERE note LIKE 'a%'"))
        show("in predicate", s.req("SELECT id FROM t WHERE id IN (1,2)"))
        show("or predicate", s.req("SELECT id FROM t WHERE id=1 OR id=2"))
        show("three conjuncts", s.req("SELECT id FROM t WHERE id>=1 AND note='a' AND id<9 ORDER BY id"))
        show("boundary equality", s.req("SELECT id FROM t WHERE id>=1 AND id<=1"))
        show("string compare", s.req("SELECT id FROM t WHERE note>'a' ORDER BY id"))
        show("partial column list", s.req("INSERT INTO t (id) VALUES (11)"))
        show("column list reordered", s.req("INSERT INTO t (emb, note, id) VALUES ('[5,5]','r',12)"))
        show("select unknown column", s.req("SELECT nope FROM t"))

        print("== vector shapes")
        show("topk unfiltered", s.req("SELECT id, L2_DISTANCE(emb,'[1,0]') AS d FROM t ORDER BY d LIMIT 3"))
        show("topk filtered", s.req("SELECT id FROM t WHERE note='a' ORDER BY L2_DISTANCE(emb,'[1,0]') LIMIT 5"))
        show("topk compound filter",
             s.req("SELECT id FROM t WHERE note='a' AND id>1 ORDER BY L2_DISTANCE(emb,'[1,0]') LIMIT 5"))
        show("topk k=1", s.req("SELECT id FROM t ORDER BY L2_DISTANCE(emb,'[1,1]') LIMIT 1"))
        show("topk k=999", s.req("SELECT id FROM t ORDER BY L2_DISTANCE(emb,'[1,1]') LIMIT 999"))
        show("topk empty filter",
             s.req("SELECT id FROM t WHERE note='zz' ORDER BY L2_DISTANCE(emb,'[1,0]') LIMIT 3"))
        show("unaliased distance column", s.req("SELECT id, L2_DISTANCE(emb,'[1,0]') FROM t LIMIT 1"))
        s.req("CREATE TABLE one (id BIGINT PRIMARY KEY, v VECTOR(1))")
        show("one dimension table", s.req("INSERT INTO one VALUES (1,'[5]')"))
        show("one dimension topk", s.req("SELECT id, L2_DISTANCE(v,'[0]') AS d FROM one ORDER BY d"))
        s.req("CREATE TABLE big (id BIGINT PRIMARY KEY, v VECTOR(768))")
        big = ",".join("0.5000" for _ in range(768))
        show("768 dimensions insert", s.req("INSERT INTO big VALUES (1,'[%s]')" % big))
        show("768 dimensions topk",
             s.req("SELECT id FROM big ORDER BY L2_DISTANCE(v,'[%s]') LIMIT 1" % big))

        print("== multi statement requests")
        show("two statements in one request",
             s.req("INSERT INTO t VALUES (99,'m','[9,9]'); SELECT id FROM t WHERE id=99"))
        show("create then use in one request",
             s.req("CREATE TABLE t2 (id BIGINT PRIMARY KEY); INSERT INTO t2 VALUES (1); "
                   "SELECT id FROM t2"))
        show("statement after a failure in one request",
             s.req("INSERT INTO t VALUES (1,'dup','[0,0]'); SELECT id FROM t WHERE id=1"))
        r = s.req("SELECT id FROM t WHERE id=99")
        print("      row 99 stored:", json.dumps(r.get("rows")))

        print("== index and ann")
        show("create vector index", s.req("CREATE VECTOR INDEX idx ON t(emb)"))
        show("create same index again", s.req("CREATE VECTOR INDEX idx ON t(emb)"))
        show("create index on missing column", s.req("CREATE VECTOR INDEX idx2 ON t(note)"))
        show("create index on missing table", s.req("CREATE VECTOR INDEX idx3 ON nope(emb)"))
        self_mode = "ann"
        s.seq += 1
        os.write(s.proc.stdin.fileno(),
                 (json.dumps({"id": s.seq, "op": "execute",
                              "sql": "SELECT id FROM t ORDER BY L2_DISTANCE(emb,'[1,0]') LIMIT 2",
                              "search_mode": self_mode}) + "\n").encode())
        print("ok    %-42s %s" % ("ann mode query", s.line()))
        s.close()
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
