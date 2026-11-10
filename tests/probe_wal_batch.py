#!/usr/bin/env python3
"""Replays multi-statement log records: a batch written as one request must survive a hard kill."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile

BIN = "/mnt/e/mvdb/build/minivectordb"


class S:
    def __init__(self, data):
        self.proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""
        self.seq = 0
        head = json.loads(self.line())
        assert head == {"type": "ready"}, head

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 16)
            if not chunk:
                raise RuntimeError("closed")
            self.buf += chunk
        out, _, self.buf = self.buf.partition(b"\n")
        return out.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql=None, op="execute"):
        self.seq += 1
        body = {"id": self.seq, "op": op}
        if sql is not None:
            body["sql"] = sql
            body["search_mode"] = "exact"
        os.write(self.proc.stdin.fileno(), (json.dumps(body, ensure_ascii=False) + "\n").encode())
        return json.loads(self.line())

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()

    def shutdown(self):
        self.req(op="shutdown")
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        return self.proc.wait(timeout=20)


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_wal_batch_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    problems = []
    try:
        s = S(data)
        s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(8));")
        # one request, whole transaction: this record is what the log stores
        r = s.req("BEGIN; INSERT INTO t VALUES (1,'a'),(2,'b'); COMMIT;")
        if r.get("ok") is not True:
            problems.append("batched transaction failed: %r" % (r,))
        # another batch that is not wrapped in a transaction (two autocommit statements)
        r = s.req("INSERT INTO t VALUES (3,'c'); INSERT INTO t VALUES (4,'d');")
        if r.get("ok") is not True:
            problems.append("batched autocommit failed: %r" % (r,))
        s.kill()

        s = S(data)
        rows = s.req("SELECT id, v FROM t ORDER BY id;").get("rows")
        print("after replay:", rows)
        if rows != [[1, "a"], [2, "b"], [3, "c"], [4, "d"]]:
            problems.append("replay produced %r" % (rows,))
        s.shutdown()

        # and once more through a clean shutdown/restart
        s = S(data)
        rows = s.req("SELECT id FROM t ORDER BY id;").get("rows")
        if rows != [[1], [2], [3], [4]]:
            problems.append("after clean restart %r" % (rows,))
        s.shutdown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for problem in problems:
        print("PROBLEM: " + problem)
    print()
    print("%d problem(s)" % len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
