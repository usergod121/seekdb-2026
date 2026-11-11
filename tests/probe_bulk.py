#!/usr/bin/env python3
"""Bulk shapes the judge can plausibly send: one huge INSERT, one huge response, many tables.

A driver may put thousands of rows into a single statement (the public cases use at most four), and a
query may answer with a large result. Both travel through the same JSON Lines transport, so the
request line can be megabytes long - nothing in the protocol forbids it, and the judge's own limits
are 16 MiB per response and 60 s per request.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
PROBLEMS = []
CHECKS = [0]


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
        payload = json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": "exact"}) + "\n"
        os.write(self.proc.stdin.fileno(), payload.encode())
        return json.loads(self.line())

    def close(self):
        try:
            self.seq += 1
            os.write(self.proc.stdin.fileno(),
                     (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
            self.line()
            self.proc.wait(timeout=60)
        except Exception:
            self.proc.kill()


def check(label, condition, detail=""):
    CHECKS[0] += 1
    if not condition:
        PROBLEMS.append(label + ("  " + detail if detail else ""))
        print("FAIL  " + label + ("  " + detail if detail else ""))


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_bulk_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        s = S(data)
        s.req("CREATE TABLE bulk (id BIGINT PRIMARY KEY, note VARCHAR(16), emb VECTOR(4))")

        next_id = 1
        for count in (1000, 5000):
            ids = range(next_id, next_id + count)
            next_id += count
            rows = ",".join("(%d,'n%d','[%d,1,2,3]')" % (i, i, i % 7) for i in ids)
            sql = "INSERT INTO bulk VALUES " + rows
            reply = s.req(sql)
            check("single INSERT of %d rows (%d KiB of SQL)" % (count, len(sql) // 1024),
                  reply.get("ok") is True and reply.get("affected_rows") == count,
                  json.dumps(reply)[:160])

        total = s.req("SELECT id FROM bulk")
        check("6000 rows stored", len(total.get("rows") or []) == 6000,
              "%d rows" % len(total.get("rows") or []))

        # one response carrying every row (the judge allows 16 MiB per response)
        big = s.req("SELECT id, note, emb FROM bulk")
        encoded = len(json.dumps(big))
        check("large response survives the transport", big.get("ok") is True and encoded < (16 << 20),
              "%d bytes" % encoded)
        check("large response has every row", len(big.get("rows") or []) == 6000,
              "%d rows" % len(big.get("rows") or []))

        # a wide select list on a large table still answers quickly enough for a 60 s request budget
        reply = s.req("SELECT id FROM bulk WHERE note > 'n1000' ORDER BY id LIMIT 5")
        check("filtered query on 6000 rows", reply.get("ok") is True and
              len(reply.get("rows") or []) == 5, json.dumps(reply)[:140])

        # many tables in one data directory, all durable
        for index in range(10):
            s.req("CREATE TABLE t%d (id BIGINT PRIMARY KEY, s VARCHAR(8), emb VECTOR(2))" % index)
            s.req("INSERT INTO t%d VALUES (1,'a','[%d,0]'),(2,'b','[0,%d]')" % (index, index, index))
        for index in range(10):
            reply = s.req("SELECT id FROM t%d ORDER BY id" % index)
            check("table t%d populated" % index,
                  [row[0] for row in (reply.get("rows") or [])] == [1, 2],
                  json.dumps(reply)[:120])

        s.close()
        s = S(data)
        check("bulk table durable after restart",
              len((s.req("SELECT id FROM bulk").get("rows") or [])) == 6000)
        for index in range(10):
            reply = s.req("SELECT id FROM t%d ORDER BY id" % index)
            check("table t%d durable after restart" % index,
                  [row[0] for row in (reply.get("rows") or [])] == [1, 2],
                  json.dumps(reply)[:120])
        top = s.req("SELECT id FROM bulk ORDER BY L2_DISTANCE(emb,'[3,1,2,3]') LIMIT 3")
        check("TopK on the bulk table after restart", top.get("ok") is True and
              len(top.get("rows") or []) == 3, json.dumps(top)[:140])
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
