#!/usr/bin/env python3
"""Reproduces the duplicate-rows-after-crash case on three transactions and dumps the data directory."""
import os
import shutil
import signal
import subprocess
import sys
import tempfile

BIN = "/mnt/e/mvdb/build/minivectordb"


class Driver:
    def __init__(self, data_dir):
        self.proc = subprocess.Popen([BIN, "--data-dir", data_dir], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""
        self.line()

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("closed")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def send(self, sql):
        os.write(self.proc.stdin.fileno(), (sql + "\n").encode("utf-8"))

    def ack(self):
        while True:
            line = self.line()
            if line.startswith("OK"):
                return line
            if line.startswith("ERROR"):
                return line

    def block(self):
        while True:
            line = self.line()
            if line.startswith("ERROR"):
                return [line]
            if line.startswith("OK") or line == "READY":
                continue
            rows = []
            while True:
                inner = self.line()
                if inner == "END":
                    return rows
                rows.append(inner.split("\t"))

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_dup_")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir)
    d = Driver(data_dir)
    print(d.ack.__self__ is not None and "started")
    d.send("CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(2));")
    print("create:", d.ack())
    for batch in range(3):
        d.send("BEGIN;")
        rows = ",".join("(%d,'[%d,0]')" % (batch * 10 + i + 1, i) for i in range(10))
        d.send("INSERT INTO t VALUES " + rows + ";")
        d.send("COMMIT;")
        print("begin:", d.ack(), "insert:", d.ack(), "commit:", d.ack())
    d.kill()

    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        print("file %-16s %d bytes" % (name, os.path.getsize(path)))

    d = Driver(data_dir)
    d.send("SELECT * FROM t;")
    rows = d.block()
    print("rows after recovery: %d" % len(rows))
    print("first 8: %r" % (rows[:8],))
    ids = [int(r[0]) for r in rows]
    from collections import Counter
    counts = Counter(ids)
    dupes = {pk: n for pk, n in counts.items() if n > 1}
    print("distinct pks: %d, duplicated: %r" % (len(counts), list(dupes.items())[:10]))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
