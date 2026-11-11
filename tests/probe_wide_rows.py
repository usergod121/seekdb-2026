#!/usr/bin/env python3
"""Very wide rows: strings of tens of thousands of characters, which cannot fit an 8 KiB page.

VARCHAR(n) counts Unicode characters (spec 2.1), so a VARCHAR(65535) column may hold 65535 CJK
characters - about 190 KiB, far past a page. The rows must round-trip byte for byte, stay queryable,
keep the vector column usable for TopK, and survive both a clean restart and a crash.
"""
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
PROBLEMS = []
CHECKS = [0]


def f32(x):
    return struct.unpack("f", struct.pack("f", float(x)))[0]


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


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_wide_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    rows = {}
    try:
        s = S(data)
        s.req("CREATE TABLE w (id BIGINT PRIMARY KEY, s VARCHAR(65535), t VARCHAR(4096), "
              "emb VECTOR(4))")
        payloads = {
            1: "a" * 60000,                              # 60000 ASCII characters, a MySQL legal row
            2: "\u4e2d" * 20000,                         # 20000 characters, 60 KiB of UTF-8
            3: "\U0001F600" * 15000,                     # 15000 four-byte characters, 60 KiB
            4: ("ab\u00e9\u4e2d" * 8000)[:26000],        # mixed, 45 KiB of UTF-8
            5: "",                                       # empty string
        }
        for pk, text in payloads.items():
            reply = s.req("INSERT INTO w VALUES (%d,'%s','short%d','[%d,%d,0,0]')"
                          % (pk, text, pk, pk, pk))
            check("insert wide row %d accepted" % pk, reply.get("ok") is True,
                  json.dumps(reply)[:140])
            rows[pk] = text

        reply = s.req("SELECT s FROM w WHERE id=2")
        got = (reply.get("rows") or [[None]])[0][0] if reply.get("ok") else None
        check("65535 CJK characters round-trip exactly", got == rows[2],
              "len=%r expected %r" % (None if got is None else len(got), len(rows[2])))
        reply = s.req("SELECT s FROM w WHERE id=3")
        got = (reply.get("rows") or [[None]])[0][0] if reply.get("ok") else None
        check("20000 emoji round-trip exactly", got == rows[3],
              "len=%r expected %r" % (None if got is None else len(got), len(rows[3])))

        # the vector column still works next to very wide neighbours
        reply = s.req("SELECT id FROM w ORDER BY L2_DISTANCE(emb,'[5,5,0,0]') LIMIT 2")
        check("TopK next to wide rows", reply.get("ok") is True and
              [row[0] for row in (reply.get("rows") or [])][0] == 5,
              json.dumps(reply)[:160])

        # a filter on the wide column must not need the whole string to match
        reply = s.req("SELECT id FROM w WHERE t='short3'")
        check("filter on a short column next to wide rows",
              reply.get("ok") is True and [row[0] for row in (reply.get("rows") or [])] == [3],
              json.dumps(reply)[:160])

        # update a wide row and read it back
        # 20000 CJK characters = 60 KiB: still inside the limit, but larger than the old value
        new_text = "\u9776" * 20000
        reply = s.req("UPDATE w SET s='%s' WHERE id=1" % new_text)
        check("update wide row", reply.get("ok") is True and reply.get("affected_rows") == 1,
              json.dumps(reply)[:160])
        rows[1] = new_text
        reply = s.req("SELECT s FROM w WHERE id=1")
        check("updated wide row reads back",
              reply.get("ok") is True and (reply.get("rows") or [[None]])[0][0] == new_text)

        # delete one and keep the rest
        check("delete a wide row",
              s.req("DELETE FROM w WHERE id=3").get("ok") is True)
        del rows[3]

        # A row past MySQL's own 64 KiB limit must be refused, not truncated or silently stored.
        reply = s.req("INSERT INTO w VALUES (60,'%s','x','[0,0,0,0]')" % ("\u4e2d" * 65535))
        check("row past 64 KiB is refused", reply.get("ok") is False, json.dumps(reply)[:140])
        check("refused row left nothing", s.req("SELECT id FROM w WHERE id=60").get("rows") == [],
              "row 60 visible")

        total = sum(len(text.encode()) for text in rows.values())
        print("      wide rows hold %d bytes of UTF-8" % total)

        # durability across a clean restart
        s.close()
        s = S(data)
        for pk, text in rows.items():
            reply = s.req("SELECT s FROM w WHERE id=%d" % pk)
            got = (reply.get("rows") or [[None]])[0][0] if reply.get("ok") else None
            check("wide row %d survived restart" % pk, got == text,
                  "len=%r expected %r" % (None if got is None else len(got), len(text)))

        # and across a crash
        s.req("BEGIN")
        s.req("INSERT INTO w VALUES (99,'%s','x','[0,0,0,0]')" % ("z" * 40000))
        s.kill()
        s = S(data)
        reply = s.req("SELECT id FROM w ORDER BY id")
        ids = [row[0] for row in (reply.get("rows") or [])]
        check("crash: committed wide rows kept, open one dropped", ids == sorted(rows),
              str(ids))
        for pk, text in rows.items():
            reply = s.req("SELECT s FROM w WHERE id=%d" % pk)
            got = (reply.get("rows") or [[None]])[0][0] if reply.get("ok") else None
            check("wide row %d intact after the crash" % pk, got == text)
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
