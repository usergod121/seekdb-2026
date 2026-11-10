#!/usr/bin/env python3
"""Shows exactly what the engine answers for each vector literal spelling."""
import json
import os
import shutil
import subprocess
import tempfile

BIN = "/mnt/e/mvdb/build/minivectordb"
LITERALS = ["[1e-3, 2E2]", "[+1,+2]", "[.5,-.5]", "[ 1 , 2 ]", "[1,2,]", "[1,,2]", "[1 2]",
            "[0.5,-0.5]", "[2E2,1]", "[1E2,3]"]

tmp = tempfile.mkdtemp(prefix="mvdb_lit_")
data = os.path.join(tmp, "data")
os.makedirs(data)
proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, bufsize=0)
buf = b""


def line():
    global buf
    while b"\n" not in buf:
        chunk = os.read(proc.stdout.fileno(), 1 << 16)
        if not chunk:
            return None
        buf += chunk
    out, _, buf = buf.partition(b"\n")
    return out.decode("utf-8", "replace").rstrip("\r")


line()  # ready
seq = 0


def req(sql):
    global seq
    seq += 1
    os.write(proc.stdin.fileno(),
             (json.dumps({"id": seq, "op": "execute", "sql": sql, "search_mode": "exact"}) + "\n").encode())
    return json.loads(line())


print("create:", req("CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(2));").get("ok"))
for index, literal in enumerate(LITERALS, 1):
    r = req("INSERT INTO t VALUES (%d, '%s');" % (index, literal))
    if r.get("ok"):
        showed = "%s" % json.dumps(r)
    else:
        err = r.get("error") or {}
        showed = "FAIL code=%s message=%s" % (err.get("code"), err.get("message"))
    print("%-14s -> %s" % (literal, showed))
print("rows:", req("SELECT id FROM t ORDER BY id;").get("rows"))
os.write(proc.stdin.fileno(), (json.dumps({"id": 999, "op": "shutdown"}) + "\n").encode())
proc.wait(timeout=20)
shutil.rmtree(tmp, ignore_errors=True)
