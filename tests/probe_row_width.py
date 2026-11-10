#!/usr/bin/env python3
"""Finds the exact row-width boundary: what fits a page, and what must be refused."""
import json
import os
import shutil
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
                              "search_mode": "exact"}, ensure_ascii=False) + "\n").encode())
        return json.loads(self.line())

    def shutdown(self):
        self.seq += 1
        os.write(self.proc.stdin.fileno(), (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
        self.line()
        self.proc.wait(timeout=20)


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_width_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    problems = []
    s = S(data)
    try:
        create = s.req("CREATE TABLE w (id BIGINT PRIMARY KEY, a VARCHAR(4096), b VARCHAR(4096));")
        print("create:", create.get("ok"))
        big = "x" * 4096
        too_wide = s.req("INSERT INTO w VALUES (1, '%s', '%s');" % (big, big))
        print("4096+4096:", too_wide.get("ok"), (too_wide.get("error") or {}).get("code"))
        after = s.req("SELECT id FROM w;")
        print("rows after the rejected insert:", after.get("rows"))
        if too_wide.get("ok") is not False:
            problems.append("a row larger than one page was accepted")
        if after.get("rows") != []:
            problems.append("the rejected insert left a row behind: %r" % (after.get("rows"),))
        ok_row = s.req("INSERT INTO w VALUES (2, '%s', '%s');" % ("y" * 4000, "z" * 4000))
        print("4000+4000:", ok_row.get("ok"), ok_row.get("affected_rows"))
        rows = s.req("SELECT id FROM w;")
        print("rows now:", rows.get("rows"))
        if ok_row.get("ok") is not True or rows.get("rows") != [[2]]:
            problems.append("a row that fits a page was refused: %r" % (ok_row,))
        # update that grows the row beyond a page must be refused without losing the old value
        grew = s.req("UPDATE w SET a = '%s' WHERE id = 2;" % big)
        print("grow update:", grew.get("ok"), (grew.get("error") or {}).get("code"))
        still = s.req("SELECT id FROM w;")
        print("rows after grow:", still.get("rows"))
        if grew.get("ok") is False and still.get("rows") != [[2]]:
            problems.append("a refused update lost the row: %r" % (still.get("rows"),))
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
