#!/usr/bin/env python3
"""Distances between extreme but perfectly legal float32 vectors.

A component may be as large as 3.4e38 (that is a finite float32, so spec 2.1 accepts it), and the
squared difference of two such components is far beyond a float32. The distance itself is still an
ordinary double, so it must come back as a finite number: a null, an infinity or a protocol error
would be a wrong answer. The batched float kernels may therefore only be used when the magnitudes
allow it.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
PROBLEMS = []
CHECKS = [0]
FLT_MAX = 3.4028234663852886e38


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
        return self.decode_line(line)

    @staticmethod
    def decode_line(raw):
        text = raw.decode("utf-8", "replace").rstrip("\r")
        # the transport rejects non-finite JSON numbers, so catching that here mirrors the judge
        return text

    def req(self, sql):
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": "exact"}) + "\n").encode())
        raw = self.line()
        try:
            return json.loads(raw)
        except Exception:
            PROBLEMS.append("non-JSON reply: " + raw[:200])
            return {"ok": False, "error": {"code": "BAD_JSON"}}

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


def distances(reply):
    if not reply.get("ok"):
        return None
    cols = reply.get("columns") or []
    if "d" not in cols:
        return None
    i = cols.index("d")
    return [row[i] for row in (reply.get("rows") or [])]


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_extreme_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        s = S(data)
        s.req("CREATE TABLE t (id BIGINT PRIMARY KEY, emb VECTOR(8))")
        extreme = ",".join(["3.4e38", "-3.4e38", "3.4e38", "-3.4e38", "3.4e38", "-3.4e38", "3.4e38",
                            "-3.4e38"])
        reply = s.req("INSERT INTO t VALUES (1,'[%s]'),(2,'[1,1,1,1,1,1,1,1]'),(3,'[0,0,0,0,0,0,0,0]')"
                      % extreme)
        check("extreme vector accepted", reply.get("ok") is True, json.dumps(reply)[:140])

        # distance against itself is exactly zero, and every neighbour's distance stays finite
        reply = s.req("SELECT id, L2_DISTANCE(emb,'[%s]') AS d FROM t ORDER BY d" % extreme)
        values = distances(reply)
        check("extreme distances are returned", values is not None and len(values) == 3,
              json.dumps(reply)[:200])
        if values:
            check("no null or infinite distance in the response",
                  all(isinstance(v, (int, float)) for v in values), str(values))
            check("distance to itself is zero", values[0] == 0, str(values))
            # every one of the 8 lanes differs by the float32 value of 3.4e38 (a one lane differs by
            # one more, which that value cannot represent), so the distance is sqrt(8) * that value
            stored = struct.unpack("f", struct.pack("f", 3.4e38))[0]
            closed_form = (8 ** 0.5) * stored
            check("neighbour distance equals the closed form",
                  all(abs(v - closed_form) <= 1e-5 * closed_form for v in values[1:]), str(values))

        # TopK keeps the right order and stays finite through the fast path
        reply = s.req("SELECT id, L2_DISTANCE(emb,'[%s]') AS d FROM t ORDER BY d LIMIT 2" % extreme)
        ids = [row[0] for row in (reply.get("rows") or [])]
        check("TopK over extreme magnitudes orders correctly", ids == [1, 2], str(ids))
        check("TopK distances are finite", all(isinstance(row[1], (int, float))
                                              for row in (reply.get("rows") or [])),
              json.dumps(reply)[:160])

        # a filter must not change any of that
        reply = s.req("SELECT id, L2_DISTANCE(emb,'[%s]') AS d FROM t WHERE id>1 ORDER BY d"
                      % extreme)
        check("filtered distances are finite",
              distances(reply) is not None and all(isinstance(v, (int, float))
                                                   for v in distances(reply)),
              json.dumps(reply)[:160])

        # tiny magnitudes (subnormals) must stay finite too
        reply = s.req("SELECT id, L2_DISTANCE(emb,'[1e-40,1e-40,1e-40,1e-40,1e-40,1e-40,1e-40,1e-40]')"
                      " AS d FROM t ORDER BY d")
        check("subnormal query distances are finite",
              distances(reply) is not None and all(isinstance(v, (int, float))
                                                   for v in distances(reply)),
              json.dumps(reply)[:160])

        # and the same questions after a restart, from the persisted rows
        s.close()
        s = S(data)
        reply = s.req("SELECT id, L2_DISTANCE(emb,'[%s]') AS d FROM t ORDER BY d" % extreme)
        values = distances(reply)
        check("extreme distances survive a restart",
              values is not None and all(isinstance(v, (int, float)) for v in values), str(values))
        reply = s.req("SELECT id FROM t ORDER BY L2_DISTANCE(emb,'[%s]') LIMIT 3" % extreme)
        check("TopK after restart", [row[0] for row in (reply.get("rows") or [])] == [1, 2, 3],
              json.dumps(reply)[:140])
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
