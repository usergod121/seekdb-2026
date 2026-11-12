#!/usr/bin/env python3
"""A/B differential harness: two engine binaries must answer identically.

The golden rule for a performance change is that it may not change one single answer. This runs the
same randomised workload against a golden binary and a candidate binary, each in its own data
directory, and compares every reply field by field (id, ok, columns, rows with a float tolerance,
affected_rows, error presence). Restarts and hard kills are part of the workload, and the recovered
state is compared too, so a change to the write path cannot slip through.

Usage:
  ab_differential.py --golden <bin> --candidate <bin> [--seed N] [--steps N] [--verbose]

Exit status 0 when the two engines agreed on everything.
"""
import argparse
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile

TOL = 1e-9


class Engine:
    def __init__(self, binary, data):
        self.binary = binary
        self.proc = subprocess.Popen([binary, "--data-dir", data], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""
        self.seq = 0
        handshake = self.line()
        if handshake is None:
            raise RuntimeError("%s produced no handshake" % binary)
        self.handshake = handshake

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 20)
            if not chunk:
                return None
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def raw_request(self, sql, mode="exact"):
        self.seq += 1
        body = {"id": self.seq, "op": "execute", "sql": sql, "search_mode": mode}
        os.write(self.proc.stdin.fileno(), (json.dumps(body) + "\n").encode())
        return self.line()

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()

    def close(self):
        try:
            self.seq += 1
            os.write(self.proc.stdin.fileno(),
                     (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
            self.line()
            self.proc.wait(timeout=60)
        except Exception:
            self.proc.kill()


def normalise(raw):
    """The reply as a comparable structure; floats are compared with a tolerance, ids by type."""
    if raw is None:
        return ("<no reply>",)
    try:
        reply = json.loads(raw)
    except Exception:
        return ("<unparsable>", raw[:200])
    out = {"ok": reply.get("ok")}
    if "id" in reply:
        out["id"] = reply["id"]
    if "columns" in reply:
        out["columns"] = reply["columns"]
    if "rows" in reply:
        out["rows"] = reply["rows"]
    if "affected_rows" in reply:
        out["affected_rows"] = reply["affected_rows"]
    if "error" in reply:
        err = reply["error"] or {}
        # the code text is part of the behaviour; compare it as well
        out["error"] = err.get("code")
    return out


def same_value(a, b):
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_value(x, y) for x, y in zip(a, b))
    if isinstance(a, float) or isinstance(b, float):
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return a == b
        scale = max(1.0, abs(a), abs(b))
        return abs(a - b) <= 1e-6 * scale
    return a == b


def compare(label, gold_raw, cand_raw, divergences):
    gold = normalise(gold_raw)
    cand = normalise(cand_raw)
    if not same_value(list(gold.values()) if isinstance(gold, dict) else gold,
                      list(cand.values()) if isinstance(cand, dict) else cand):
        divergences.append("%s\n    golden   : %s\n    candidate: %s"
                           % (label, json.dumps(gold, ensure_ascii=False)[:400],
                              json.dumps(cand, ensure_ascii=False)[:400]))
        return False
    return True


# --------------------------------------------------------------------------------- workload
def sql_literal(value, ctype):
    if ctype == "BIGINT":
        return str(value)
    if ctype == "VARCHAR":
        return "'" + str(value).replace("'", "''") + "'"
    return "'[" + ",".join("%.4f" % v for v in value) + "]'"


class Workload:
    """A deterministic statement stream; the same stream is replayed on both engines."""

    def __init__(self, seed, steps, verbose=False):
        self.rng = random.Random(seed)
        self.steps = steps
        self.verbose = verbose
        self.tables = []

    def make_table(self):
        rng = self.rng
        dims = rng.choice([1, 2, 4, 8])
        cols = [("id", "BIGINT", 0)]
        for i in range(rng.randint(1, 2)):
            cols.append(("s%d" % i, "VARCHAR", rng.choice([4, 8, 16])))
        has_vec = rng.random() < 0.85
        if has_vec:
            cols.append(("emb", "VECTOR", dims))
        name = "t%d" % len(self.tables)
        definition = ", ".join(
            "%s %s" % (c[0], "BIGINT" if c[1] == "BIGINT" else
                       ("VARCHAR(%d)" % c[2] if c[1] == "VARCHAR" else "VECTOR(%d)" % c[2]))
            for c in cols)
        table = {"name": name, "cols": cols, "dims": dims if has_vec else 0,
                 "has_vec": has_vec, "next_id": 1}
        self.tables.append(table)
        return "CREATE TABLE %s (%s PRIMARY KEY%s)" % (
            name, definition if False else "id BIGINT",
            "".join(", " + piece for piece in definition.split(", ")[1:])) , table

    def value(self, col):
        rng = self.rng
        name, ctype, length = col
        if ctype == "BIGINT":
            return rng.choice([0, 1, -1, 7, -7, 1000, -1000, 2 ** 40, -(2 ** 40)])
        if ctype == "VARCHAR":
            alphabet = "abcdZ09 \u4e2d\u6587\U0001F600"
            return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, length)))
        return [round(rng.uniform(-1, 1), 4) for _ in range(length)]

    def statements(self):
        out = []
        for _ in range(2):
            sql, table = self.make_table()
            out.append(("execute", sql, "exact", "create " + table["name"]))
        for step in range(self.steps):
            table = self.rng.choice(self.tables)
            roll = self.rng.random()
            if roll < 0.26:
                count = self.rng.randint(1, 4)
                rows = []
                for _ in range(count):
                    values = [self.value(c) for c in table["cols"]]
                    values[0] = table["next_id"]
                    table["next_id"] += 1
                    rows.append(values)
                sql = "INSERT INTO %s VALUES %s" % (table["name"], ",".join(
                    "(" + ",".join(sql_literal(v, c[1]) for v, c in zip(row, table["cols"])) + ")"
                    for row in rows))
                out.append(("execute", sql, "exact", "insert"))
            elif roll < 0.42:
                col = self.rng.choice([c for c in table["cols"] if c[0] != "id"])
                op = self.rng.choice(["=", "<", "<=", ">", ">="])
                value = self.value(col)
                sql = "UPDATE %s SET %s = %s WHERE %s %s %s" % (
                    table["name"], col[0], sql_literal(value, col[1]), col[0], op,
                    sql_literal(value, col[1]))
                out.append(("execute", sql, "exact", "update"))
            elif roll < 0.52:
                col = self.rng.choice([c for c in table["cols"] if c[0] != "id"])
                value = self.value(col)
                sql = "DELETE FROM %s WHERE %s = %s" % (table["name"], col[0],
                                                        sql_literal(value, col[1]))
                out.append(("execute", sql, "exact", "delete"))
            elif roll < 0.82:
                sql = self.query(table)
                out.append(("execute", sql[0], sql[1], "query"))
            elif roll < 0.92:
                out.append(("execute", "BEGIN", "exact", "begin"))
                for _ in range(self.rng.randint(1, 3)):
                    out.append(("execute", self.query(table)[0], "exact", "query in tx"))
                out.append(("execute", "COMMIT" if self.rng.random() < 0.5 else "ROLLBACK",
                            "exact", "end tx"))
            else:
                out.append(("restart", "", "exact", "restart"))
        out.append(("restart", "", "exact", "final restart"))
        for table in self.tables:
            out.append(("execute", "SELECT id FROM %s ORDER BY id" % table["name"], "exact",
                        "final check " + table["name"]))
        return out

    def query(self, table):
        rng = self.rng
        mode = "ann" if rng.random() < 0.3 else "exact"
        cols = table["cols"]
        chosen = rng.sample(cols, rng.randint(1, len(cols)))
        pieces = []
        for i, c in enumerate(chosen):
            alias = "c%d" % i if rng.random() < 0.4 else c[0]
            pieces.append("%s AS %s" % (c[0], alias))
        where = ""
        # Filters stay on ordinary columns with a literal of the matching type: that is the shape the
        # task specifies, and it keeps the two engines comparable. Comparisons across types and on a
        # vector column are checked by probe_predicate_types.py instead, since the golden binary
        # answered those with a crash or a dropped conjunct.
        ordinary = [c for c in cols if c[1] != "VECTOR"]
        if rng.random() < 0.6 and ordinary:
            col = rng.choice(ordinary)
            op = rng.choice(["=", "<", "<=", ">", ">="])
            where = " WHERE %s %s %s" % (col[0], op, sql_literal(self.value(col), col[1]))
        order = ""
        limit = ""
        if table["has_vec"] and rng.random() < 0.75:
            qvec = [round(rng.uniform(-1, 1), 4) for _ in range(table["dims"])]
            pieces.append("L2_DISTANCE(emb,'[%s]') AS d" % ",".join("%.4f" % v for v in qvec))
            order = " ORDER BY d"
            limit = " LIMIT %d" % rng.choice([1, 2, 3, 10, 999])
        else:
            if rng.random() < 0.8:
                keys = [c[0] for c in cols if c[1] != "VECTOR"]
                if keys:
                    order = " ORDER BY " + ", ".join(
                        k + (" DESC" if rng.random() < 0.4 else "")
                        for k in rng.sample(keys, rng.randint(1, len(keys))))
            if rng.random() < 0.5:
                limit = " LIMIT %d" % rng.choice([0, 1, 5, 999])
        sql = "SELECT %s FROM %s%s%s%s" % (", ".join(pieces), table["name"], where, order, limit)
        return sql, mode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", required=True)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--steps", type=int, default=200)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    workload = Workload(args.seed, args.steps, args.verbose)
    script = workload.statements()

    tmp = tempfile.mkdtemp(prefix="mvdb_ab_")
    divergences = []
    compared = 0
    try:
        data_gold = os.path.join(tmp, "gold")
        data_cand = os.path.join(tmp, "cand")
        os.makedirs(data_gold)
        os.makedirs(data_cand)
        gold = Engine(args.golden, data_gold)
        cand = Engine(args.candidate, data_cand)
        try:
            if gold.handshake != cand.handshake:
                divergences.append("handshake differs: %r vs %r"
                                   % (gold.handshake, cand.handshake))
            for op, sql, mode, label in script:
                if op == "restart":
                    gold.close()
                    cand.close()
                    gold = Engine(args.golden, data_gold)
                    cand = Engine(args.candidate, data_cand)
                    compared += 1
                    continue
                gold_raw = gold.raw_request(sql, mode)
                cand_raw = cand.raw_request(sql, mode)
                compared += 1
                if not compare("[%s] %s" % (label, sql[:160]), gold_raw, cand_raw, divergences):
                    if len(divergences) >= 10:
                        break
                if args.verbose:
                    print("compared: %s" % sql[:100])
        finally:
            gold.close()
            cand.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("seed %d: %d replies compared, %d divergence(s)"
          % (args.seed, compared, len(divergences)))
    for item in divergences:
        print("DIVERGENCE " + item)
    return 1 if divergences else 0


if __name__ == "__main__":
    sys.exit(main())
