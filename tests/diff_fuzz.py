#!/usr/bin/env python3
"""Randomized differential test: the same random SQL runs against the engine and a reference model.

The reference model implements the documented semantics (BIGINT compares numerically, VARCHAR compares by
Unicode code point, VECTOR is float32 and L2_DISTANCE is the plain Euclidean distance, ordinary ties
break on ascending primary key, LIMIT 0 yields nothing, no ORDER BY leaves the row order free), so a
mismatch is a real defect rather than a difference of opinion.
"""
import argparse
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
CATEGORIES = ["\u7532", "\u4e59", "\u4e19"]
CONTENTS = ["\u4e00", "\u4e8c", "\u4e09", "\u56db", "\u4e94"]
QUERIES = [[0.0, 0.0], [1.0, 0.0], [0.0, 2.0], [-1.5, 0.5], [3.0, 4.0]]
COMPARE_OPS = ["=", "<>", "<", "<=", ">", ">="]


def f32(x):
    return struct.unpack("f", struct.pack("f", x))[0]


def vec_lit(v):
    return "[" + ", ".join(("%g" % c) for c in v) + "]"


# ---- reference model ------------------------------------------------------

class Model:
    def __init__(self):
        self.rows = {}  # pk -> dict of column -> value (float32 for the vector)

    def insert(self, vals):
        self.rows[vals["id"]] = {
            "id": vals["id"],
            "category": vals["category"],
            "content": vals["content"],
            "embedding": [f32(c) for c in vals["embedding"]],
        }
        return 1

    def matching(self, pred):
        return [row for row in self.rows.values() if pred(row)]

    def update(self, pred, assign):
        rows = self.matching(pred)
        for row in rows:
            for key, value in assign.items():
                if key == "embedding":
                    value = [f32(c) for c in value]
                row[key] = value
        return len(rows)

    def delete(self, pred):
        rows = self.matching(pred)
        for row in rows:
            del self.rows[row["id"]]
        return len(rows)

    def select(self, proj, pred, order, limit):
        rows = self.matching(pred)
        if order is not None:
            keyfunc, asc = order
            # Ordinary ties break on ascending primary key in both directions: sorting by primary key
            # first and then by the key with a stable sort keeps that order inside equal keys.
            rows.sort(key=lambda r: r["id"])
            rows.sort(key=keyfunc, reverse=not asc)
        out = [[row[name] for name in proj] for row in rows]
        if limit is not None:
            out = out[:limit]
        return out


def l2(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


# ---- engine session ------------------------------------------------------

class Session:
    def __init__(self, data_dir):
        self.proc = subprocess.Popen(
            [BIN, "--data-dir", data_dir],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
        )
        self.buf = b""
        self._expect(b"READY")

    def _line(self):
        while b"\n" not in self.buf:
            chunk = self.proc.stdout.read(1)
            if not chunk:
                raise RuntimeError("engine closed its output")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def _expect(self, marker):
        line = self._line()
        if line.encode("utf-8") != marker:
            raise RuntimeError("expected %r, got %r" % (marker, line))

    def run(self, sql):
        self.proc.stdin.write((sql + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        out = {"kind": None, "columns": None, "rows": [], "affected": None, "error": None}
        while True:
            line = self._line()
            if line == "READY":
                continue
            if line.startswith("OK "):
                out["kind"] = "ok"
                out["affected"] = int(line.split()[1])
                return out
            if line == "OK":
                out["kind"] = "ok"
                out["affected"] = 0
                return out
            if line.startswith("ERROR"):
                out["kind"] = "error"
                out["error"] = line
                return out
            if line == "END":
                out["kind"] = "rows"
                return out
            if out["columns"] is None:
                out["columns"] = [] if line == "" else line.split("\t")
            else:
                out["rows"].append([] if line == "" else line.split("\t"))

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# ---- generator -----------------------------------------------------------

class Case:
    """One random statement: SQL text, its model effect, and how to check the result."""

    def __init__(self, sql, kind, apply=None, proj=None, pred=None, order=None, limit=None,
                 unordered=False):
        self.sql = sql
        self.kind = kind
        self.apply = apply
        self.proj = proj
        self.pred = pred
        self.order = order
        self.limit = limit
        self.unordered = unordered


def compare(left, op, right):
    if op == "=":
        return left == right
    if op == "<>":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    return left >= right


def gen_pred(rng):
    """A WHERE clause plus its reference predicate."""
    terms = []
    preds = []
    for _ in range(rng.choice([1, 1, 1, 2])):
        if rng.random() < 0.55:
            op = rng.choice(COMPARE_OPS)
            value = rng.randint(-1, 8)
            terms.append("id %s %d" % (op, value))
            preds.append(lambda r, op=op, value=value: compare(r["id"], op, value))
        else:
            name = rng.choice(["category", "content"])
            pool = CATEGORIES if name == "category" else CONTENTS
            value = rng.choice(pool)
            op = rng.choice(COMPARE_OPS)
            terms.append("%s %s '%s'" % (name, op, value))
            preds.append(lambda r, name=name, op=op, value=value: compare(r[name], op, value))
    return " AND ".join(terms), (lambda r: all(p(r) for p in preds))


def gen_insert(rng, used_pks):
    free = [pk for pk in range(1, 12) if pk not in used_pks]
    if not free:
        return None
    count = min(len(free), rng.choice([1, 1, 2, 3]))
    pks = rng.sample(free, count)
    rows = []
    for pk in pks:
        rows.append({
            "id": pk,
            "category": rng.choice(CATEGORIES),
            "content": rng.choice(CONTENTS),
            "embedding": [f32(rng.choice([0, 1, 2, 3, -1])), f32(rng.choice([0, 1, 2, 0.5]))],
        })
    sql = "INSERT INTO docs VALUES " + ", ".join(
        "(%d, '%s', '%s', '%s')" % (r["id"], r["category"], r["content"], vec_lit(r["embedding"]))
        for r in rows
    ) + ";"
    return sql, rows


def gen_update(rng, model):
    if not model.rows:
        return None
    where, pred = gen_pred(rng)
    if rng.random() < 0.5:
        value = rng.choice(CONTENTS)
        sql = "UPDATE docs SET content = '%s' WHERE %s;" % (value, where)
        assign = {"content": value}
    else:
        v = [f32(rng.choice([0, 1, 2, 3])), f32(rng.choice([0, 1, 2]))]
        sql = "UPDATE docs SET embedding = '%s' WHERE %s;" % (vec_lit(v), where)
        assign = {"embedding": v}
    return Case(sql, "write", apply=("update", pred, assign))


def gen_delete(rng, model):
    if not model.rows:
        return None
    where, pred = gen_pred(rng)
    return Case("DELETE FROM docs WHERE %s;" % where, "write", apply=("delete", pred))


def gen_select(rng):
    style = rng.choices(
        ["id_order", "category_order", "alias_l2", "plain", "star", "limit0", "no_order"],
        weights=[3, 2, 3, 2, 1, 1, 2],
    )[0]
    where, pred = gen_pred(rng)
    if rng.random() < 0.7:
        where_sql = " WHERE %s" % where
    else:
        where_sql = ""
        pred = lambda r: True

    if style == "id_order":
        limit = rng.choice([None, 0, 1, 2, 5, 50])
        desc = rng.random() < 0.35
        sql = "SELECT id FROM docs%s ORDER BY id%s%s;" % (
            where_sql, " DESC" if desc else "", "" if limit is None else " LIMIT %d" % limit)
        return Case(sql, "query", proj=["id"], pred=pred,
                    order=(lambda r: r["id"], not desc), limit=limit)

    if style == "category_order":
        limit = rng.choice([None, 1, 3, 10])
        desc = rng.random() < 0.4
        sql = "SELECT id FROM docs%s ORDER BY category%s%s;" % (
            where_sql, " DESC" if desc else "", "" if limit is None else " LIMIT %d" % limit)
        return Case(sql, "query", proj=["id"], pred=pred,
                    order=(lambda r: r["category"], not desc), limit=limit)

    if style == "alias_l2":
        q = rng.choice(QUERIES)
        limit = rng.choice([1, 2, 3, 10])
        desc = rng.random() < 0.25
        sql = ("SELECT id, L2_DISTANCE(embedding, '%s') AS distance FROM docs%s "
               "ORDER BY distance%s LIMIT %d;" % (
                   vec_lit(q), where_sql, " DESC" if desc else "", limit))
        return Case(sql, "query", proj=["id", ("l2", q)], pred=pred,
                    order=(lambda r, q=q: l2(r["embedding"], [f32(c) for c in q]), not desc),
                    limit=limit)

    if style == "star":
        sql = "SELECT * FROM docs%s;" % where_sql
        return Case(sql, "query", proj=["id", "category", "content", "embedding"], pred=pred,
                    unordered=True)

    if style == "limit0":
        sql = "SELECT id FROM docs%s LIMIT 0;" % where_sql
        return Case(sql, "query", proj=["id"], pred=pred, limit=0)

    if style == "no_order":
        sql = "SELECT id, content FROM docs%s;" % where_sql
        return Case(sql, "query", proj=["id", "content"], pred=pred, unordered=True)

    sql = "SELECT id, category FROM docs%s;" % where_sql
    return Case(sql, "query", proj=["id", "category"], pred=pred, unordered=True)


# ---- comparison ----------------------------------------------------------

def cell_ok(expected, actual):
    if isinstance(expected, tuple):  # ("l2", distance)
        try:
            got = float(actual)
        except ValueError:
            return False
        diff = abs(got - expected[1])
        return diff <= 1e-5 or diff <= 1e-5 * abs(expected[1])
    if isinstance(expected, (int, float)):
        try:
            return abs(float(actual) - expected) <= 1e-5
        except ValueError:
            return False
    if isinstance(expected, list):
        try:
            got = [float(c) for c in actual.strip("[]").split(",")]
        except ValueError:
            return False
        return len(got) == len(expected) and all(
            abs(g - e) <= 1e-5 for g, e in zip(got, expected))
    return expected == actual


def cells_match(expected_row, got_row):
    if len(expected_row) != len(got_row):
        return False
    return all(cell_ok(e, a) for e, a in zip(expected_row, got_row))


def check(case, result, model):
    problems = []
    if result["kind"] == "error":
        return ["engine reported %s" % result["error"]]
    if case.kind == "write":
        action = case.apply
        if result["kind"] != "ok":
            return ["expected a write acknowledgement, got %s" % result["kind"]]
        if action[0] == "update":
            want = model.update(action[1], action[2])
        else:
            want = model.delete(action[1])
        if result["affected"] != want:
            problems.append("affected_rows %d != %d" % (result["affected"], want))
        return problems

    if result["kind"] != "rows":
        return ["expected rows, got %s" % result["kind"]]
    want_names = ["distance" if isinstance(name, tuple) else name for name in case.proj]
    if result["columns"] != want_names:
        problems.append("columns %r != %r" % (result["columns"], want_names))
    expected = model.select(
        ["embedding" if isinstance(name, tuple) else name for name in case.proj],
        case.pred, case.order, case.limit)
    want_rows = []
    for row in expected:
        cells = []
        for name, value in zip(case.proj, row):
            if isinstance(name, tuple):
                cells.append(("l2", l2(value, [f32(c) for c in name[1]])))
            else:
                cells.append(value)
        want_rows.append(cells)

    got = result["rows"]
    if len(want_rows) != len(got):
        problems.append("row count %d != %d" % (len(got), len(want_rows)))
    if case.unordered:
        remaining = list(got)
        for want in want_rows:
            for index, candidate in enumerate(remaining):
                if cells_match(want, candidate):
                    del remaining[index]
                    break
            else:
                problems.append("missing unordered row %r" % (want,))
        if remaining:
            problems.append("unexpected unordered rows %r" % (remaining,))
        return problems

    for index, (want, have) in enumerate(zip(want_rows, got)):
        if not cells_match(want, have):
            problems.append("row %d %r != %r" % (index, have, want))
            break
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=4)
    ap.add_argument("--steps", type=int, default=80)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--dump", action="store_true")
    args = ap.parse_args()

    total = 0
    failures = 0
    for seed in range(args.seeds):
        rng = random.Random(seed)
        tmp = tempfile.mkdtemp(prefix="mvdb_diff_")
        data_dir = os.path.join(tmp, "data")
        os.makedirs(data_dir)
        session = Session(data_dir)
        model = Model()
        try:
            setup = ("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), "
                     "content VARCHAR(16), embedding VECTOR(2));")
            if session.run(setup)["kind"] == "error":
                print("setup failed")
                return 1
            for step in range(args.steps):
                pick = rng.random()
                if pick < 0.3:
                    made = gen_insert(rng, set(model.rows))
                    if made is None:
                        continue
                    sql, rows = made
                    result = session.run(sql)
                    total += 1
                    if result["kind"] == "error":
                        failures += 1
                        print("seed %d step %d: %s -> %s" % (seed, step, sql, result["error"]))
                        continue
                    if result["affected"] != len(rows):
                        failures += 1
                        print("seed %d step %d: %s -> affected %d != %d" % (
                            seed, step, sql, result["affected"], len(rows)))
                    for row in rows:
                        model.insert(row)
                    continue
                if pick < 0.45:
                    case = gen_update(rng, model)
                elif pick < 0.6:
                    case = gen_delete(rng, model)
                else:
                    case = gen_select(rng)
                if case is None:
                    continue
                result = session.run(case.sql)
                total += 1
                problems = check(case, result, model)
                if problems:
                    failures += 1
                    print("seed %d step %d: %s" % (seed, step, case.sql))
                    for problem in problems:
                        print("    " + problem)
                    if args.dump:
                        print("    table:")
                        for pk in sorted(model.rows):
                            print("      %r" % (model.rows[pk],))
                        if case.order is not None:
                            keyfunc = case.order[0]
                            for pk in sorted(model.rows, key=lambda p: keyfunc(model.rows[p])):
                                print("      key(%d) = %.17g" % (pk, keyfunc(model.rows[pk])))
                elif args.verbose:
                    print("ok seed %d step %d: %s" % (seed, step, case.sql))
        finally:
            session.close()
            shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("%d statement(s), %d mismatch(es)" % (total, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
