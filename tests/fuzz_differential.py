#!/usr/bin/env python3
"""Randomised differential tester.

Generates legal statements against random tables and compares every reply with an independent Python
model of the semantics the task specifies. Statements cover inserts, single and multi row updates and
deletes, projections with aliases, conjunctive predicates, ordering (including ties and multi-key
ORDER BY, which must break ties on the primary key), LIMIT, vector TopK with and without a filter in
exact and ann mode, transactions that commit or roll back, and restarts in the middle of the run.

Usage: fuzz_differential.py [--seed N] [--statements N] [--verbose]
Exit status 0 when no divergence was found.
"""
import argparse
import json
import os
import random
import shutil
import signal
import struct
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
TOL = 1e-5


def f32(x):
    return struct.unpack("f", struct.pack("f", float(x)))[0]


class Engine:
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

    def req(self, sql, mode="exact"):
        self.seq += 1
        os.write(self.proc.stdin.fileno(),
                 (json.dumps({"id": self.seq, "op": "execute", "sql": sql,
                              "search_mode": mode}) + "\n").encode())
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


class Model:
    """tables: name -> {"cols": [(name, type, len)], "pk": name, "rows": {pk: [values]}}"""

    def __init__(self):
        self.tables = {}

    def create(self, name, cols, pk):
        self.tables[name] = {"cols": cols, "pk": pk, "rows": {}}

    def col(self, table, name):
        for i, (cname, _ctype, _clen) in enumerate(self.tables[table]["cols"]):
            if cname == name:
                return i
        raise KeyError(name)

    def pk_index(self, table):
        return self.col(table, self.tables[table]["pk"])

    def insert(self, table, rows):
        pk = self.pk_index(table)
        for row in rows:
            if row[pk] in self.tables[table]["rows"]:
                raise ValueError("duplicate")
        for row in rows:
            self.tables[table]["rows"][row[pk]] = list(row)
        return len(rows)

    def rows_matching(self, table, pred):
        out = []
        for key in sorted(self.tables[table]["rows"]):
            cells = self.tables[table]["rows"][key]
            if pred is None or pred(cells):
                out.append(cells)
        return out

    def update(self, table, sets, pred):
        matched = self.rows_matching(table, pred)
        for cells in matched:
            for col, value in sets:
                cells[self.col(table, col)] = value
        return len(matched)

    def delete(self, table, pred):
        matched = self.rows_matching(table, pred)
        pk = self.pk_index(table)
        for cells in matched:
            del self.tables[table]["rows"][cells[pk]]
        return len(matched)

    def select(self, table, projections, pred, order, limit):
        """projections: [("col", name)|("l2", col, qvec)]"""
        pk = self.pk_index(table)
        rows = self.rows_matching(table, pred)

        def value(cells, key):
            if key[0] == "l2":
                stored = cells[self.col(table, key[1])]
                total = 0.0
                for a, b in zip(stored, key[2]):
                    d = a - f32(b)
                    total += d * d
                return total ** 0.5
            return cells[self.col(table, key[1])]

        items = []
        for cells in rows:
            record = []
            for proj in projections:
                if proj[0] == "col":
                    record.append(cells[self.col(table, proj[1])])
                else:
                    record.append(value(cells, ("l2", proj[1], proj[2])))
            items.append((cells, record))
        if order:
            items.sort(key=lambda item: item[0][pk])
            for key, desc in reversed(order):
                items.sort(key=lambda item, key=key: value(item[0], key), reverse=desc)
        out = [record for _, record in items]
        return out[:limit] if limit is not None else out


# --------------------------------------------------------------------------- value/text helpers
def fmt_vector(values):
    return "[" + ",".join("%.4f" % v for v in values) + "]"


def sql_literal(value, ctype):
    if ctype == "BIGINT":
        return str(value)
    if ctype == "VARCHAR":
        # Quotes, backslashes and control characters are escaped so every statement stays on one line
        # and transports exactly the string the model stores.
        text = str(value).replace("\\", "\\\\").replace("'", "''")
        text = text.replace("\n", "\\n").replace("\t", "\\t")
        return "'" + text + "'"
    return "'" + fmt_vector(value) + "'"


def comparable(a, b):
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(comparable(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) <= TOL or abs(float(a) - float(b)) <= TOL * abs(float(b))
    return a == b


def rows_equal(expected, got):
    if expected is None or got is None:
        return False
    if len(expected) != len(got):
        return False
    return all(len(e) == len(g) and all(comparable(gv, ev) for ev, gv in zip(e, g))
               for e, g in zip(expected, got))


# --------------------------------------------------------------------------- fuzz driver
class Fuzzer:
    def __init__(self, seed, verbose=False):
        self.rng = random.Random(seed)
        self.verbose = verbose
        self.model = Model()
        self.tmp = tempfile.mkdtemp(prefix="mvdb_fuzz_")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        self.engine = Engine(self.data)
        self.divergences = []
        self.checks = 0
        self.ddl = []
        self.tables = []

    # -- schema ------------------------------------------------------------
    def make_schema(self):
        rng = self.rng
        dims = rng.choice([1, 2, 4, 8, 16])
        cols = [("id", "BIGINT", 0)]
        nv = rng.randint(1, 2)
        for i in range(nv):
            cols.append(("s%d" % i, "VARCHAR", rng.choice([4, 8, 16, 32])))
        has_vec = rng.random() < 0.8
        if has_vec:
            cols.append(("emb", "VECTOR", dims))
        name = "t%d" % len(self.tables)
        sql = "CREATE TABLE %s (%s BIGINT PRIMARY KEY%s)" % (
            name, "id",
            "".join(", %s %s" % (c[0], "VARCHAR(%d)" % c[2] if c[1] == "VARCHAR"
                                 else "VECTOR(%d)" % c[2]) for c in cols[1:]))
        reply = self.engine.req(sql)
        self.expect(reply, {}, "create table " + name)
        self.model.create(name, cols, "id")
        self.ddl.append(sql)
        self.tables.append({"name": name, "cols": cols, "dims": dims if has_vec else 0,
                            "has_vec": has_vec})
        return self.tables[-1]

    def table(self):
        return self.rng.choice(self.tables)

    # -- value generation --------------------------------------------------
    def random_value(self, col, used=None):
        cname, ctype, clen = col
        rng = self.rng
        if ctype == "BIGINT":
            choices = [0, 1, -1, 7, -7, 12345, -12345, 2 ** 62, -(2 ** 62), 2 ** 63 - 1, -(2 ** 63)]
            return rng.choice(choices)
        if ctype == "VARCHAR":
            alphabet = "abcdZ09 \u4e2d\u6587\U0001F600 \u00e9"
            length = rng.randint(0, clen)
            return "".join(rng.choice(alphabet) for _ in range(length))
        return [round(rng.uniform(-1, 1), 4) for _ in range(clen)]

    def next_pk(self, table, taken=None):
        rows = self.model.tables[table["name"]]["rows"]
        if taken is None:
            taken = set()
        candidate = (max(rows) + 1) if rows else 1
        while candidate in rows or candidate in taken:
            candidate += 1
        taken.add(candidate)
        return candidate

    def insert_values(self, table):
        return [self.random_value(c) for c in table["cols"]]

    # -- statement builders ------------------------------------------------
    def pred_spec(self, table, allow_vec=False):
        """Returns (sql_fragment, python_predicate) or (None, None)."""
        rng = self.rng
        cols = [c for c in table["cols"] if c[1] != "VECTOR"]
        if not cols or rng.random() < 0.25:
            return None, None
        parts = []
        preds = []
        for _ in range(rng.randint(1, 3)):
            col = rng.choice(cols)
            op = rng.choice(["=", "<", "<=", ">", ">="])
            value = self.random_value(col)
            parts.append("%s %s %s" % (col[0], op, sql_literal(value, col[1])))

            def make(col=col, op=op, value=value):
                def pred(cells):
                    actual = cells[self.model.col(table["name"], col[0])]
                    rhs = f32(value) if col[1] == "VECTOR" else value
                    if op == "=":
                        return actual == rhs
                    if op == "<":
                        return actual < rhs
                    if op == "<=":
                        return actual <= rhs
                    if op == ">":
                        return actual > rhs
                    return actual >= rhs
                return pred
            preds.append(make())

        def combined(cells):
            return all(p(cells) for p in preds)
        return " AND ".join(parts), combined

    def build_select(self, table):
        rng = self.rng
        cols = table["cols"]
        mode = "ann" if rng.random() < 0.3 else "exact"
        where_sql, pred = self.pred_spec(table)
        # projection
        if rng.random() < 0.25:
            projections = [("col", c[0]) for c in cols]
            select_sql, names = "*", [c[0] for c in cols]
        else:
            chosen = rng.sample(cols, rng.randint(1, len(cols)))
            projections, names, pieces = [], [], []
            for i, c in enumerate(chosen):
                alias = "c%d" % i if rng.random() < 0.4 else c[0]
                pieces.append("%s AS %s" % (c[0], alias))
                projections.append(("col", c[0]))
                names.append(alias)
            select_sql = ", ".join(pieces)
        # vector TopK
        order = []
        limit = None
        if table["has_vec"] and rng.random() < 0.7:
            qvec = [round(rng.uniform(-1, 1), 4) for _ in range(table["dims"])]
            alias = "d"
            select_sql += ", L2_DISTANCE(emb,'%s') AS %s" % (fmt_vector(qvec), alias)
            projections.append(("l2", "emb", qvec))
            names.append(alias)
            key = ("l2", "emb", qvec)
            order.append((key, False))
            limit = rng.choice([1, 2, 3, 5, 10, 999])
        else:
            if rng.random() < 0.8:
                # a vector column has no ordering (the engine rejects it on purpose), so only the
                # ordinary columns become sort keys
                keys = [c[0] for c in cols if c[1] != "VECTOR"]
                for _ in range(rng.randint(1, 2)):
                    order.append(((rng.choice(["col"]) and ("col", rng.choice(keys))),
                                  rng.random() < 0.4))
            if rng.random() < 0.6:
                limit = rng.choice([0, 1, 2, 3, 5, 999])
        sql = "SELECT %s FROM %s" % (select_sql, table["name"])
        if where_sql:
            sql += " WHERE " + where_sql
        if order:
            pieces = []
            for key, desc in order:
                if key[0] == "l2":
                    pieces.append("d" + (" DESC" if desc else ""))
                else:
                    pieces.append(key[1] + (" DESC" if desc else ""))
            sql += " ORDER BY " + ", ".join(pieces)
        if limit is not None:
            sql += " LIMIT %d" % limit
        expected = self.model.select(table["name"], projections, pred,
                                     [(k, d) for k, d in order], limit)
        return sql, {"columns": names, "rows": expected}, mode, bool(where_sql)

    # -- checks ------------------------------------------------------------
    def expect(self, reply, expected, label, ordered=True):
        self.checks += 1
        if expected.get("error"):
            ok = reply.get("ok") is False
            if not ok:
                self.fail(label, "expected an error", reply)
            return ok
        if reply.get("ok") is not True:
            self.fail(label, "unexpected error", reply)
            return False
        if "affected_rows" in expected:
            got = reply.get("affected_rows")
            if got != expected["affected_rows"]:
                self.fail(label, "affected_rows %r != %r" % (got, expected["affected_rows"]), reply)
                return False
            return True
        if "columns" in expected:
            got_cols = reply.get("columns")
            if got_cols != expected["columns"]:
                self.fail(label, "columns %r != %r" % (got_cols, expected["columns"]), reply)
                return False
            got_rows = reply.get("rows") or []
            want = expected["rows"]
            if ordered:
                same = rows_equal(want, got_rows)
            else:
                remaining = list(got_rows)
                same = len(want) == len(got_rows)
                for row in want:
                    hit = next((i for i, cand in enumerate(remaining)
                                if rows_equal([row], [cand])), None)
                    if hit is None:
                        same = False
                        break
                    remaining.pop(hit)
            if not same:
                self.fail(label, "rows %r != %r" % (got_rows[:4], want[:4]), reply)
                return False
            return True
        return True

    def fail(self, label, message, reply):
        text = "%s | %s | reply=%s" % (label[:220], message, json.dumps(reply)[:200])
        self.divergences.append(text)
        print("DIVERGENCE  " + text)

    # -- operations --------------------------------------------------------
    def op_insert(self, table):
        count = self.rng.randint(1, 4)
        rows = []
        taken = set()
        for _ in range(count):
            values = self.insert_values(table)
            values[0] = self.next_pk(table, taken)
            rows.append(values)
        sql = "INSERT INTO %s VALUES %s" % (table["name"], ",".join(
            "(" + ",".join(sql_literal(v, c[1]) for v, c in zip(row, table["cols"])) + ")"
            for row in rows))
        n = self.model.insert(table["name"], rows)
        self.expect(self.engine.req(sql), {"affected_rows": n}, sql)
        return sql

    def op_update(self, table):
        rng = self.rng
        cols = [c for c in table["cols"] if c[0] != "id" and c[1] != "VECTOR"] or \
               [c for c in table["cols"] if c[0] != "id"]
        if not cols:
            return None
        target = rng.choice(cols)
        value = self.random_value(target)
        where_sql, pred = self.pred_spec(table)
        sql = "UPDATE %s SET %s = %s" % (table["name"], target[0], sql_literal(value, target[1]))
        if where_sql:
            sql += " WHERE " + where_sql
        n = self.model.update(table["name"], [(target[0], value)], pred)
        self.expect(self.engine.req(sql), {"affected_rows": n}, sql)
        return sql

    def op_delete(self, table):
        where_sql, pred = self.pred_spec(table)
        if where_sql is None and self.rng.random() < 0.7:
            return None
        sql = "DELETE FROM %s" % table["name"] + ((" WHERE " + where_sql) if where_sql else "")
        n = self.model.delete(table["name"], pred)
        self.expect(self.engine.req(sql), {"affected_rows": n}, sql)
        return sql

    def op_select(self, table):
        sql, expected, mode, has_where = self.build_select(table)
        reply = self.engine.req(sql, mode)
        self.expect(reply, expected, sql + (" [" + mode + "]"), ordered=("ORDER BY" in sql))
        return sql

    def op_transaction(self, table):
        rng = self.rng
        # deepcopy, not a JSON round trip: json turns the integer primary keys into strings, which
        # then breaks every later key comparison.
        import copy as _copy
        snapshot = _copy.deepcopy(self.model.tables)
        self.expect(self.engine.req("BEGIN"), {}, "BEGIN")
        ops = []
        for _ in range(rng.randint(1, 3)):
            choice = rng.random()
            if choice < 0.45:
                ops.append(self.op_insert(table))
            elif choice < 0.75:
                ops.append(self.op_update(table))
            else:
                ops.append(self.op_select(table))
        if rng.random() < 0.5:
            self.expect(self.engine.req("COMMIT"), {}, "COMMIT")
        else:
            self.expect(self.engine.req("ROLLBACK"), {}, "ROLLBACK")
            self.model.tables = snapshot
        return ops

    def op_restart(self, crash=False):
        if crash:
            self.engine.kill()
        else:
            self.engine.close()
        self.engine = Engine(self.data)
        for table in self.tables:
            reply = self.engine.req("SELECT id FROM %s ORDER BY id" % table["name"])
            want = sorted(self.model.tables[table["name"]]["rows"])
            got = [row[0] for row in (reply.get("rows") or [])]
            self.expect(reply, {"columns": ["id"], "rows": [[k] for k in want]},
                        "after restart: keys of " + table["name"])

    def verify_all(self):
        for table in self.tables:
            sql, expected, mode, _ = self.build_select(table)
            self.expect(self.engine.req(sql, mode), expected, "final " + sql,
                        ordered=("ORDER BY" in sql))

    def run(self, statements):
        for _ in range(1, 3):
            self.make_schema()
        rng = self.rng
        for step in range(statements):
            table = self.table()
            roll = rng.random()
            if roll < 0.30:
                self.op_insert(table)
            elif roll < 0.45:
                self.op_update(table)
            elif roll < 0.55:
                self.op_delete(table)
            elif roll < 0.80:
                self.op_select(table)
            elif roll < 0.92:
                self.op_transaction(table)
            elif roll < 0.97:
                self.op_restart()
            else:
                self.op_restart(crash=True)
            if self.divergences and len(self.divergences) >= 8:
                break
        self.verify_all()
        self.engine.close()
        return self.divergences


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--statements", type=int, default=300)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    fuzzer = Fuzzer(args.seed, args.verbose)
    try:
        divergences = fuzzer.run(args.statements)
    finally:
        shutil.rmtree(fuzzer.tmp, ignore_errors=True)
    print("seed %d: %d checks, %d divergence(s)" % (args.seed, fuzzer.checks, len(divergences)))
    return 1 if divergences else 0


if __name__ == "__main__":
    sys.exit(main())
