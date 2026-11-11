#!/usr/bin/env python3
"""Table shapes a formal case is likely to use, including several wide text columns in one row."""
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


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_shapes_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        s = S(data)

        # 1. a primary key only table
        check("create pk-only table",
              s.req("CREATE TABLE k (id BIGINT PRIMARY KEY)").get("ok") is True)
        check("insert into pk-only table",
              s.req("INSERT INTO k VALUES (3),(1),(2)").get("ok") is True)
        reply = s.req("SELECT * FROM k ORDER BY id")
        check("select from pk-only table", reply.get("rows") == [[1], [2], [3]],
              json.dumps(reply)[:120])
        check("update in pk-only table",
              s.req("UPDATE k SET id=9 WHERE id=3").get("ok") is False)   # primary key update
        check("delete from pk-only table",
              s.req("DELETE FROM k WHERE id=2").get("ok") is True)
        check("pk-only table after delete",
              (s.req("SELECT id FROM k ORDER BY id").get("rows") or []) == [[1], [3]])

        # 2. a text table with no vector column
        check("create text-only table",
              s.req("CREATE TABLE txt (id BIGINT PRIMARY KEY, a VARCHAR(64), b VARCHAR(64))").get("ok")
              is True)
        check("insert text rows",
              s.req("INSERT INTO txt VALUES (1,'x','y'),(2,'','')").get("ok") is True)
        reply = s.req("SELECT a, b FROM txt ORDER BY id")
        check("select text rows", reply.get("rows") == [["x", "y"], ["", ""]],
              json.dumps(reply)[:120])

        # 3. four wide text columns in one row - around 16 KiB, what the old 8 KiB page rejected
        check("create wide text table",
              s.req("CREATE TABLE wide (id BIGINT PRIMARY KEY, a VARCHAR(4096), b VARCHAR(4096), "
                    "c VARCHAR(4096), d VARCHAR(4096))").get("ok") is True)
        a, b, c, d = ("a" * 4000, "b" * 4000, "c" * 4000, "d" * 4000)
        reply = s.req("INSERT INTO wide VALUES (1,'%s','%s','%s','%s')" % (a, b, c, d))
        check("16 KiB row accepted", reply.get("ok") is True, json.dumps(reply)[:140])
        reply = s.req("SELECT a, b, c, d FROM wide WHERE id=1")
        row = (reply.get("rows") or [[None]])[0]
        check("16 KiB row round-trips", row == [a, b, c, d], str([len(x or "") for x in row]))
        # Chinese content of the same size, where characters and bytes differ
        zh = "\u4e2d" * 1300   # 1300 characters, 3900 bytes
        check("wide CJK row accepted",
              s.req("UPDATE wide SET a='%s' WHERE id=1" % zh).get("ok") is True)
        reply = s.req("SELECT a FROM wide WHERE id=1")
        check("wide CJK row round-trips", (reply.get("rows") or [[None]])[0][0] == zh)

        # 4. a vector column next to wide text, in one row
        check("create mixed table",
              s.req("CREATE TABLE mix (id BIGINT PRIMARY KEY, body VARCHAR(4096), "
                    "emb VECTOR(8))").get("ok") is True)
        check("insert mixed wide row",
              s.req("INSERT INTO mix VALUES (1,'%s','[1,2,3,4,5,6,7,8]')" % ("z" * 4000)).get("ok")
              is True)
        reply = s.req("SELECT id FROM mix ORDER BY L2_DISTANCE(emb,'[1,2,3,4,5,6,7,8]') LIMIT 1")
        check("TopK on a table with wide text", reply.get("rows") == [[1]], json.dumps(reply)[:120])

        s.close()
        s = S(data)
        reply = s.req("SELECT a FROM wide WHERE id=1")
        check("wide row durable after restart", (reply.get("rows") or [[None]])[0][0] == zh)
        check("pk-only table durable after restart",
              (s.req("SELECT id FROM k ORDER BY id").get("rows") or []) == [[1], [3]])
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
