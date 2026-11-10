#!/usr/bin/env python3
"""Checks that one request carrying several statements behaves like the same statements sent one by one."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

BIN = "/mnt/e/mvdb/build/minivectordb"


class Session:
    def __init__(self, data_dir):
        self.proc = subprocess.Popen([BIN, "--data-dir", data_dir], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""
        self.seq = 0
        if json.loads(self.line()) != {"type": "ready"}:
            raise RuntimeError("bad handshake")

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("closed")
            self.buf += chunk
        out, _, self.buf = self.buf.partition(b"\n")
        return out.decode().rstrip("\r")

    def req(self, sql):
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": "exact"}, ensure_ascii=False) + "\n").encode())
        return json.loads(self.line())

    def shutdown(self):
        self.seq += 1
        os.write(self.proc.stdin.fileno(), (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
        reply = json.loads(self.line())
        self.proc.wait(timeout=20)
        return reply


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_multi_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    problems = []
    s = Session(data)
    try:
        cases = [
            ("CREATE TABLE docs (id BIGINT PRIMARY KEY, v BIGINT);"
             "INSERT INTO docs VALUES (1,10),(2,20);",
             "create+insert in one request"),
            ("BEGIN; INSERT INTO docs VALUES (3,30); COMMIT;", "transaction in one request"),
            ("SELECT id, v FROM docs ORDER BY id;", "query after batched transaction"),
        ]
        for sql, label in cases:
            reply = s.req(sql)
            print("%-42s -> ok=%s rows=%r affected=%r columnset=%r" % (
                label, reply.get("ok"), reply.get("rows"), reply.get("affected_rows"),
                reply.get("columns")))
            if reply.get("ok") is not True:
                problems.append(label + ": not ok")
        # rollback inside one request must undo the batch
        reply = s.req("BEGIN; DELETE FROM docs WHERE id >= 1; ROLLBACK;")
        if reply.get("ok") is not True:
            problems.append("batched rollback: not ok")
        reply = s.req("SELECT id FROM docs ORDER BY id;")
        if reply.get("rows") != [[1], [2], [3]]:
            problems.append("batched rollback left %r" % (reply.get("rows"),))
        # a failing statement inside a batch stops the batch and reports the error
        reply = s.req("INSERT INTO docs VALUES (4,40); INSERT INTO docs VALUES (1,99);")
        if reply.get("ok") is not False:
            problems.append("batched duplicate did not fail")
        reply = s.req("SELECT id FROM docs ORDER BY id;")
        if reply.get("rows") != [[1], [2], [3], [4]]:
            problems.append("after failed batch rows=%r" % (reply.get("rows"),))
        s.shutdown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for problem in problems:
        print("FAIL " + problem)
    print()
    print("%d problem(s)" % len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
