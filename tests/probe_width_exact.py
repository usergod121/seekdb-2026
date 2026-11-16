#!/usr/bin/env python3
"""Does the coarse path stay exact when the vector width is not a multiple of 32?

The wide int8 kernel accumulates the cross term for the first (dims/32)*32 dimensions and finishes the
tail with a scalar loop. Anything that mixes the two must not double count. This probe builds a table
large enough to use the coarse path, at several widths, and compares every answer with brute force.
"""
import json
import subprocess
import sys
import tempfile

BIN = sys.argv[1] if len(sys.argv) > 1 else "/mnt/e/mvdb/build-v12/minivectordb"
ROWS = 3000
K = 5


def sql_vec(values):
    return "[" + ",".join("%.4f" % v for v in values) + "]"


def gen(rows, dims, seed=1234):
    state = seed
    out = []
    for _ in range(rows * dims):
        state = (state * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        out.append(((state >> 33) & 0xFFFFFF) / 8388608.0 - 1.0)
    return [out[i * dims:(i + 1) * dims] for i in range(rows)]


def main():
    failures = 0
    for dims in (4, 8, 16, 31, 32, 33, 48, 64, 96, 128):
        with tempfile.TemporaryDirectory() as data:
            proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            proc.stdout.readline()

            def send(sql):
                proc.stdin.write(json.dumps({"id": 1, "op": "execute", "sql": sql}) + "\n")
                proc.stdin.flush()
                return json.loads(proc.stdout.readline())

            send("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), emb VECTOR(%d))" % dims)
            rows = gen(ROWS, dims)
            for start in range(0, ROWS, 100):
                chunk = rows[start:start + 100]
                body = ",".join("(%d,'c%d','%s')" % (start + i + 1, (start + i) % 8, sql_vec(v))
                                for i, v in enumerate(chunk))
                r = send("INSERT INTO docs VALUES " + body)
                if not r.get("ok"):
                    sys.exit("insert failed at dims=%d: %s" % (dims, r))
            query = gen(1, dims, seed=999)[0]
            for label, sql in (
                ("plain", "SELECT id FROM docs ORDER BY L2_DISTANCE(emb,'%s') LIMIT %d" % (sql_vec(query), K)),
                ("filtered", "SELECT id FROM docs WHERE category='c3' ORDER BY L2_DISTANCE(emb,'%s') LIMIT %d" % (sql_vec(query), K)),
            ):
                got = send(sql)
                if not got.get("ok"):
                    print("dims=%-4d %-9s query failed: %s" % (dims, label, got))
                    failures += 1
                    continue
                ids = [row[0] for row in got["rows"]]
                want = []
                for i, v in enumerate(rows):
                    if label == "filtered" and i % 8 != 3:
                        continue
                    d = sum((v[j] - query[j]) ** 2 for j in range(dims))
                    want.append((d, i + 1))
                want.sort()
                want_ids = [pk for _, pk in want[:K]]
                ok = ids == want_ids
                if not ok:
                    failures += 1
                print("dims=%-4d %-9s %s" % (dims, label, "ok" if ok else "MISMATCH got=%s want=%s" % (ids, want_ids)))
            proc.stdin.close()
            proc.wait(timeout=60)
    print("widths with problems:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
