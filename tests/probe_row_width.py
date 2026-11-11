#!/usr/bin/env python3
"""Row width: what the engine stores, and where it must refuse.

A row must fit one page, and the page is sized to the largest row MySQL itself allows (a VARCHAR
column may hold 65535 bytes), so the boundary is the 64 KiB row limit rather than the 8 KiB page the
heap used to have. Wide rows must round-trip, and a row past the limit must be refused cleanly -
no partial row, no lost value when an update tries to grow a row too far.
"""
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
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql):
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": "exact"}) + "\n").encode())
        return json.loads(self.line())

    def shutdown(self):
        try:
            self.seq += 1
            os.write(self.proc.stdin.fileno(),
                     (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
            self.line()
            self.proc.wait(timeout=20)
        except Exception:
            self.proc.kill()


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_width_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    problems = []
    s = S(data)
    try:
        create = s.req("CREATE TABLE w (id BIGINT PRIMARY KEY, a VARCHAR(65535), b VARCHAR(4096));")
        print("create:", create.get("ok"))
        if create.get("ok") is not True:
            problems.append("create failed: %r" % (create,))

        # ordinary rows keep working
        ok_row = s.req("INSERT INTO w VALUES (1, '%s', '%s');" % ("y" * 4000, "z" * 4000))
        print("4000+4000:", ok_row.get("ok"), ok_row.get("affected_rows"))
        if ok_row.get("ok") is not True:
            problems.append("an 8 KiB row was refused: %r" % (ok_row,))

        # a row of 60 KiB is legal for MySQL and must be stored and returned unchanged
        wide_text = "w" * 60000
        wide = s.req("INSERT INTO w VALUES (2, '%s', 'b');" % wide_text)
        print("60000 characters:", wide.get("ok"), (wide.get("error") or {}).get("code"))
        if wide.get("ok") is not True:
            problems.append("a 60 KiB row was refused: %r" % (wide,))
        back = s.req("SELECT a FROM w WHERE id = 2;")
        got = (back.get("rows") or [[None]])[0][0] if back.get("ok") else None
        if got != wide_text:
            problems.append("the wide row did not round-trip (%r characters)"
                            % (None if got is None else len(got),))

        # past the 64 KiB row limit the statement must fail and leave nothing behind
        over = s.req("INSERT INTO w VALUES (3, '%s', '%s');" % ("x" * 65535, "q" * 4096))
        print("65535+4096:", over.get("ok"), (over.get("error") or {}).get("code"))
        if over.get("ok") is not False:
            problems.append("a row past the 64 KiB limit was accepted")
        rows = s.req("SELECT id FROM w ORDER BY id;")
        print("rows:", rows.get("rows"))
        if [row[0] for row in (rows.get("rows") or [])] != [1, 2]:
            problems.append("the refused insert left something behind: %r" % (rows.get("rows"),))

        # growing a row past the limit must fail without losing the stored value
        grew = s.req("UPDATE w SET a = '%s' WHERE id = 1;" % ("x" * 65000))
        print("grow update:", grew.get("ok"), (grew.get("error") or {}).get("code"))
        still = s.req("SELECT a FROM w WHERE id = 1;")
        got = (still.get("rows") or [[None]])[0][0] if still.get("ok") else None
        if grew.get("ok") is False and got != "y" * 4000:
            problems.append("a refused growth lost or changed the stored value")
        if grew.get("ok") is True and got != "x" * 65000:
            problems.append("a growth that succeeded stored the wrong value")

        # and the wide row survives a restart
        s.shutdown()
        s = S(data)
        back = s.req("SELECT a FROM w WHERE id = 2;")
        got = (back.get("rows") or [[None]])[0][0] if back.get("ok") else None
        if got != wide_text:
            problems.append("the wide row did not survive a restart")
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
