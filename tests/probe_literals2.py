#!/usr/bin/env python3
"""Diagnoses each vector literal spelling one at a time, with a clean row each time."""
import json
import os
import shutil
import subprocess
import tempfile

BIN = "/mnt/e/mvdb/build/minivectordb"
LITERALS = ["[1E2,3]", "[1e2,3]", "[2E2,1]", "[1 2]", "[1,,2]", "[1,2,]", "[1,2,3]", "[1,2]"]

tmp = tempfile.mkdtemp(prefix="mvdb_lit2_")
data = os.path.join(tmp, "data")
os.makedirs(data)
proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, bufsize=0)
state = {"buf": b""}


def line():
    while b"\n" not in state["buf"]:
        chunk = os.read(proc.stdout.fileno(), 1 << 16)
        if not chunk:
            return None
        state["buf"] += chunk
    out, _, state["buf"] = state["buf"].partition(b"\n")
    return out.decode("utf-8", "replace").rstrip("\r")


line()
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
        print("%-12s -> accepted (affected %s)" % (literal, r.get("affected_rows")))
    else:
        err = r.get("error") or {}
        print("%-12s -> %s / %s" % (literal, err.get("code"), err.get("message")))
print("rows:", req("SELECT id FROM t ORDER BY id;").get("rows"))
os.write(proc.stdin.fileno(), (json.dumps({"id": 999, "op": "shutdown"}) + "\n").encode())
proc.wait(timeout=20)
shutil.rmtree(tmp, ignore_errors=True)
