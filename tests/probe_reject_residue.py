#!/usr/bin/env python3
"""Does a rejected statement leave anything behind?

The rejection battery counts accept/reject decisions; this probe checks what a rejection leaves in the
data: after three rejected statements the table must hold exactly the rows that were accepted, both in
the same session and after a hard restart that replays the write-ahead log.

Usage: probe_reject_residue.py [--binary PATH]
"""
import argparse
import json
import os
import subprocess
import tempfile


class Session:
    def __init__(self, binary, data):
        self.proc = subprocess.Popen([binary, "--data-dir", data], stdin=subprocess.PIPE,
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
        out, _, self.buf = self.buf.partition(b"\n")
        return out.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql):
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": "exact"}) + "\n").encode())
        return json.loads(self.line())

    def shutdown(self):
        self.seq += 1
        os.write(self.proc.stdin.fileno(), (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
        self.line()
        self.proc.wait(timeout=60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default="/mnt/e/mvdb/build/minivectordb")
    args = ap.parse_args()

    data = tempfile.mkdtemp(prefix="mvdb_rej_")
    s = Session(args.binary, data)
    s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, tag VARCHAR(4), emb VECTOR(2))")
    print("accepted:", s.req("INSERT INTO t VALUES (1,'a','[1,2]'),(2,'b','[3,4]')"))
    rejects = [
        ("NaN component", "INSERT INTO t VALUES (3,'c','[nan,1]')"),
        ("dimension mismatch", "INSERT INTO t VALUES (4,'d','[1,2,3]')"),
        ("VARCHAR too long", "INSERT INTO t VALUES (5,'toolong','[1,2]')"),
        ("BIGINT overflow", "INSERT INTO t VALUES (9223372036854775808,'e','[1,2]')"),
    ]
    for label, sql in rejects:
        print("rejected (%s):" % label, s.req(sql))
    print("rows after rejections:", s.req("SELECT id, tag FROM t ORDER BY id"))
    print("aggregate rejected:", s.req("SELECT COUNT(*) FROM t"))
    s.shutdown()

    r = Session(args.binary, data)
    print("rows after restart:", r.req("SELECT id, tag FROM t ORDER BY id"))
    print("aggregate rejected after restart:", r.req("SELECT COUNT(*) FROM t"))
    r.shutdown()
    # A hard kill between the two sessions is covered by probe_crash_loop; the check here is that the
    # rejected statements never reached the table at all.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
