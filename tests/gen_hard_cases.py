#!/usr/bin/env python3
"""Generates a harder sibling of the public case set, in the package's own .test/.result format.

The public package is described as "simplified samples" of the formal cases ("不能用评测仓库中的
同名正式用例替代它们"), so the formal set most likely contains the same case names with more rows,
more conditions and more transitions. This generator writes such cases into tests/hard_cases/ and
computes every expected result with an independent Python model of the specified semantics, so an
engine disagreement is a real finding rather than a recording of the engine's own output.

Format: JSON Lines protocol, <case>.test is one directive or statement per line, <case>.result is a
JSON array with one expected response per statement (the shape protocol_check/_assertions.py checks).
"""
import json
import os
import struct
import sys

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hard_cases")
TOL = 1e-5


def f32(x):
    return struct.unpack("f", struct.pack("f", float(x)))[0]


# ----------------------------------------------------------------------------- model of the spec
class Model:
    """BIGINT / VARCHAR(n) / VECTOR(n) columns, one BIGINT primary key, one vector column at most."""

    def __init__(self):
        self.tables = {}   # name -> {"cols": [(name, type, len, pk)], "rows": {pk: [cells]}}
        self.snapshot = None

    def begin(self):
        import copy
        self.snapshot = copy.deepcopy(self.tables)

    def commit(self):
        self.snapshot = None

    def rollback(self):
        if self.snapshot is not None:
            self.tables = self.snapshot
        self.snapshot = None

    def create(self, name, cols):
        self.tables[name] = {"cols": cols, "rows": {}}

    def col_index(self, table, col):
        for i, c in enumerate(self.tables[table]["cols"]):
            if c[0] == col:
                return i
        raise KeyError(col)

    def pk_index(self, table):
        for i, c in enumerate(self.tables[table]["cols"]):
            if c[3]:
                return i
        raise KeyError("no primary key")

    def insert(self, table, rows):
        pk = self.pk_index(table)
        for row in rows:
            if row[pk] in self.tables[table]["rows"]:
                raise ValueError("duplicate key %r" % (row[pk],))
        for row in rows:
            cells = []
            for (cname, ctype, clen, _), value in zip(self.tables[table]["cols"], row):
                if ctype == "VECTOR":
                    if not isinstance(value, list) or len(value) != clen:
                        raise ValueError("dimension mismatch")
                    cells.append([f32(v) for v in value])
                elif ctype == "BIGINT":
                    cells.append(int(value))
                else:
                    text = str(value)
                    if len(text) > clen and all(ord(ch) < 128 for ch in text):
                        raise ValueError("value too long")
                    if sum(1 for ch in text if not (0x80 <= ord(ch) < 0xC0)) > clen:
                        raise ValueError("value too long")
                    cells.append(text)
            self.tables[table]["rows"][cells[pk]] = cells
        return len(rows)

    def matching(self, table, pred):
        pk = self.pk_index(table)
        rows = []
        for key in sorted(self.tables[table]["rows"]):
            cells = self.tables[table]["rows"][key]
            if pred is None or pred(self, table, cells):
                rows.append(cells)
        return rows

    def update(self, table, sets, pred):
        rows = self.matching(table, pred)
        changed = 0
        for cells in rows:
            new = list(cells)
            for col, value in sets:
                idx = self.col_index(table, col)
                cname, ctype, clen, _ = self.tables[table]["cols"][idx]
                new[idx] = [f32(v) for v in value] if ctype == "VECTOR" else value
            if new != cells:
                self.tables[table]["rows"][new[self.pk_index(table)]] = new
                changed += 1
        return changed

    def delete(self, table, pred):
        rows = self.matching(table, pred)
        for cells in rows:
            del self.tables[table]["rows"][cells[self.pk_index(table)]]
        return len(rows)

    def select(self, table, projections, pred, order, limit):
        """projections: list of ("col", name) or ("l2", column, qvec, alias)."""
        rows = self.matching(table, pred)
        pk = self.pk_index(table)

        def value(cells, key):
            if isinstance(key, tuple) and key[0] == "l2":
                _, col, qvec = key
                stored = cells[self.col_index(table, col)]
                total = 0.0
                for a, b in zip(stored, qvec):
                    d = a - f32(b)
                    total += d * d
                return total ** 0.5
            return cells[self.col_index(table, key)]

        items = []
        for cells in rows:
            record = []
            for proj in projections:
                if proj[0] == "col":
                    record.append(cells[self.col_index(table, proj[1])])
                else:
                    record.append(value(cells, ("l2", proj[1], proj[2])))
            items.append((cells, record))
        if order is not None:
            # Stable multi-pass sort from the least significant key to the most significant one, with
            # the primary key ascending as the final (innermost) key: ties keep ascending id order in
            # both directions, which is what spec 2.2 asks for.
            items.sort(key=lambda item: item[0][pk])
            for key, desc in reversed(order):
                items.sort(key=lambda item, key=key: value(item[0], key), reverse=desc)
        out = [record for _, record in items]
        if limit is not None:
            out = out[:limit]
        return out


def response_rows(columns, rows):
    return {"columns": list(columns), "rows": rows}


# ----------------------------------------------------------------------------- case definitions
def case_hard_crud(m):
    m.create("docs", [("id", "BIGINT", 0, True), ("grp", "VARCHAR", 8, False),
                      ("note", "VARCHAR", 16, False)])
    stmts = []

    def add(sql, expect):
        stmts.append((sql, expect))

    add("CREATE TABLE docs (id BIGINT PRIMARY KEY, grp VARCHAR(8), note VARCHAR(16))", {})
    add("INSERT INTO docs VALUES (5,'b','five'),(1,'a','one'),(3,'b','three'),(2,'a','two'),"
        "(4,'c','four')",
        {"affected_rows": (m.insert("docs", [[5, "b", "five"], [1, "a", "one"], [3, "b", "three"],
                                            [2, "a", "two"], [4, "c", "four"]]))})
    expect = m.select("docs", [("col", "id"), ("col", "note")], None, [("id", False)], None)
    add("SELECT id, note FROM docs ORDER BY id", response_rows(["id", "note"], expect))
    changed = m.update("docs", [("note", "UPD")], lambda mm, t, c: c[1] == "a")
    add("UPDATE docs SET note='UPD' WHERE grp='a'", {"affected_rows": changed})
    expect = m.select("docs", [("col", "id"), ("col", "grp"), ("col", "note")], None,
                      [("id", False)], None)
    add("SELECT id, grp, note FROM docs ORDER BY id", response_rows(["id", "grp", "note"], expect))
    changed = m.update("docs", [("grp", "z")], lambda mm, t, c: c[0] > 3 and c[1] == "b")
    add("UPDATE docs SET grp='z' WHERE id>3 AND grp='b'", {"affected_rows": changed})
    removed = m.delete("docs", lambda mm, t, c: c[2] == "three")
    add("DELETE FROM docs WHERE note='three'", {"affected_rows": removed})
    expect = m.select("docs", [("col", "id")], lambda mm, t, c: c[1] == "z", [("id", False)], None)
    add("SELECT id FROM docs WHERE grp='z' ORDER BY id", response_rows(["id"], expect))
    expect = m.select("docs", [("col", "id"), ("col", "grp")], None, [("id", True)], 2)
    add("SELECT id, grp FROM docs ORDER BY id DESC LIMIT 2", response_rows(["id", "grp"], expect))
    return stmts


def case_hard_order(m):
    m.create("t", [("id", "BIGINT", 0, True), ("name", "VARCHAR", 8, False),
                   ("score", "BIGINT", 0, False)])
    stmts = []

    def add(sql, expect=None):
        stmts.append((sql, expect if expect is not None else {}))

    add("CREATE TABLE t (id BIGINT PRIMARY KEY, name VARCHAR(8), score BIGINT)")
    rows = [[7, "bb", 10], [2, "aa", 10], [9, "aa", 5], [4, "cc", 10], [1, "bb", 5], [6, "aa", 10]]
    add("INSERT INTO t VALUES (7,'bb',10),(2,'aa',10),(9,'aa',5),(4,'cc',10),(1,'bb',5),(6,'aa',10)",
        {"affected_rows": m.insert("t", rows)})
    # ties on name are ordered by primary key ascending
    expect = m.select("t", [("col", "id"), ("col", "name")], None, [("name", False)], None)
    add("SELECT id, name FROM t ORDER BY name", response_rows(["id", "name"], expect))
    expect = m.select("t", [("col", "id"), ("col", "name")], None, [("name", True)], None)
    add("SELECT id, name FROM t ORDER BY name DESC", response_rows(["id", "name"], expect))
    expect = m.select("t", [("col", "id"), ("col", "name"), ("col", "score")], None,
                      [("score", False)], 3)
    add("SELECT id, name, score FROM t ORDER BY score LIMIT 3",
        response_rows(["id", "name", "score"], expect))
    expect = m.select("t", [("col", "id")], lambda mm, t, c: c[2] >= 10, [("score", True), ("id", False)],
                      2)
    add("SELECT id FROM t WHERE score>=10 ORDER BY score DESC, id ASC LIMIT 2",
        response_rows(["id"], expect))
    expect = m.select("t", [("col", "id")], None, [("name", False)], 0)
    add("SELECT id FROM t ORDER BY name LIMIT 0", response_rows(["id"], expect))
    expect = m.select("t", [("col", "id")], lambda mm, t, c: False, [("name", False)], None)
    add("SELECT id FROM t WHERE name='zz' ORDER BY name", response_rows(["id"], expect))
    return stmts


def case_hard_empty(m):
    m.create("e", [("id", "BIGINT", 0, True), ("v", "VECTOR", 3, False)])
    stmts = []
    stmts.append(("CREATE TABLE e (id BIGINT PRIMARY KEY, v VECTOR(3))", {}))
    stmts.append(("SELECT id FROM e ORDER BY id", response_rows(["id"], [])))
    stmts.append(("SELECT id, L2_DISTANCE(v,'[0,0,0]') AS d FROM e ORDER BY d LIMIT 5",
                  response_rows(["id", "d"], [])))
    stmts.append(("DELETE FROM e WHERE id=1", {"affected_rows": 0}))
    stmts.append(("UPDATE e SET v='[1,0,0]' WHERE id=1", {"affected_rows": 0}))
    m.insert("e", [[2, [3.0, 4.0, 0.0]], [1, [0.0, 0.0, 1.0]]])
    stmts.append(("INSERT INTO e VALUES (2,'[3,4,0]'),(1,'[0,0,1]')", {"affected_rows": 2}))
    expect = m.select("e", [("col", "id"), ("l2", "v", [0.0, 0.0, 0.0], "d")], None,
                      [(("l2", "v", [0.0, 0.0, 0.0]), False)], None)
    stmts.append(("SELECT id, L2_DISTANCE(v,'[0,0,0]') AS d FROM e ORDER BY d",
                  response_rows(["id", "d"], expect)))
    expect = m.select("e", [("col", "id")], None, [("id", True)], None)
    stmts.append(("SELECT id FROM e ORDER BY id DESC", response_rows(["id"], expect)))
    return stmts


def case_hard_strings(m):
    m.create("s", [("id", "BIGINT", 0, True), ("txt", "VARCHAR", 12, False)])
    stmts = []

    def add(sql, expect):
        stmts.append((sql, expect))

    add("CREATE TABLE s (id BIGINT PRIMARY KEY, txt VARCHAR(12))", {})
    values = [
        (1, 'a"b'),
        (2, "c'd"),
        (3, "e\\f"),
        (4, "g\th"),
        (5, "i\nj"),
        (6, "\u4e2d\u6587\u5b57"),
        (7, "\U0001F600\U0001F601"),
        (8, "\u00e9\u00e8"),
        (9, ""),
        (10, "\u4e2d\u6587\u5b57\u7b26 10"),
    ]
    def literal(text):
        out = ["'"]
        for ch in text:
            if ch == "'":
                out.append("''")
            elif ch == "\\":
                out.append("\\\\")
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
        out.append("'")
        return "".join(out)

    parts = ",".join("(%d,%s)" % (i, literal(t)) for i, t in values)
    add("INSERT INTO s VALUES " + parts, {"affected_rows": m.insert("s", [list(v) for v in values])})
    expect = m.select("s", [("col", "id"), ("col", "txt")], None, [("id", False)], None)
    add("SELECT id, txt FROM s ORDER BY id", response_rows(["id", "txt"], expect))
    # code point order: 'a"b' < 'c'd' < 'e\f' < 'g\th' < 'i\nj' < 'éè' < '中文' ...
    expect = m.select("s", [("col", "id")], lambda mm, t, c: c[1] > "\u00e9\u00e8",
                      [("txt", False)], None)
    add("SELECT id FROM s WHERE txt > '\u00e9\u00e8' ORDER BY txt",
        response_rows(["id"], expect))
    expect = m.select("s", [("col", "id")], lambda mm, t, c: c[1] == "", [("id", False)], None)
    add("SELECT id FROM s WHERE txt='' ORDER BY id", response_rows(["id"], expect))
    return stmts


def case_hard_bigint(m):
    m.create("b", [("id", "BIGINT", 0, True), ("v", "BIGINT", 0, False)])
    stmts = []
    stmts.append(("CREATE TABLE b (id BIGINT PRIMARY KEY, v BIGINT)", {}))
    values = [[9223372036854775807, -9223372036854775808], [-9223372036854775808, 0],
              [0, 9223372036854775807], [-1, 1], [1, -1]]
    stmts.append(("INSERT INTO b VALUES (9223372036854775807,-9223372036854775808),"
                  "(-9223372036854775808,0),(0,9223372036854775807),(-1,1),(1,-1)",
                  {"affected_rows": m.insert("b", values)}))
    expect = m.select("b", [("col", "id"), ("col", "v")], None, [("id", False)], None)
    stmts.append(("SELECT id, v FROM b ORDER BY id", response_rows(["id", "v"], expect)))
    expect = m.select("b", [("col", "id")], lambda mm, t, c: c[1] < 0, [("id", False)], None)
    stmts.append(("SELECT id FROM b WHERE v<0 ORDER BY id", response_rows(["id"], expect)))
    stmts.append(("INSERT INTO b VALUES (9223372036854775808,0)", {"error": "value_out_of_range"}))
    stmts.append(("INSERT INTO b VALUES (1,0)", {"error": "duplicate_key"}))
    expect = m.select("b", [("col", "id")], lambda mm, t, c: c[0] >= -1, [("id", False)], None)
    stmts.append(("SELECT id FROM b WHERE id>=-1 ORDER BY id", response_rows(["id"], expect)))
    return stmts


def case_hard_vector(m):
    m.create("v", [("id", "BIGINT", 0, True), ("grp", "VARCHAR", 4, False),
                   ("emb", "VECTOR", 2, False)])
    stmts = []

    def add(sql, expect=None):
        stmts.append((sql, expect if expect is not None else {}))

    add("CREATE TABLE v (id BIGINT PRIMARY KEY, grp VARCHAR(4), emb VECTOR(2))")
    rows = [[10, "a", [0.0, 0.0]], [2, "a", [1.0, 0.0]], [7, "a", [1.0, 0.0]],
            [5, "b", [0.0, 1.0]], [1, "b", [0.0, 0.0]], [9, "c", [2.0, 2.0]]]
    add("INSERT INTO v VALUES (10,'a','[0,0]'),(2,'a','[1,0]'),(7,'a','[1,0]'),(5,'b','[0,1]'),"
        "(1,'b','[0,0]'),(9,'c','[2,2]')", {"affected_rows": m.insert("v", rows)})
    q = [0.0, 0.0]
    # ties on the distance break on the primary key, so ids 1 and 10 (both distance 0) come first
    expect = m.select("v", [("col", "id"), ("l2", "emb", q, "d")], None,
                      [(("l2", "emb", q), False)], None)
    add("SELECT id, L2_DISTANCE(emb,'[0,0]') AS d FROM v ORDER BY d",
        response_rows(["id", "d"], expect))
    expect = m.select("v", [("col", "id"), ("l2", "emb", q, "d")],
                      lambda mm, t, c: c[1] == "a", [(("l2", "emb", q), False)], 2)
    add("SELECT id, L2_DISTANCE(emb,'[0,0]') AS d FROM v WHERE grp='a' ORDER BY d LIMIT 2",
        response_rows(["id", "d"], expect))
    add("--mode ann", None)
    expect = m.select("v", [("col", "id")], lambda mm, t, c: c[1] == "b",
                      [(("l2", "emb", [0.0, 1.0]), False)], 5)
    add("SELECT id FROM v WHERE grp='b' ORDER BY L2_DISTANCE(emb,'[0,1]') LIMIT 5",
        response_rows(["id"], expect))
    add("--mode exact", None)
    # the index has to keep reflecting updates and deletes
    add("CREATE VECTOR INDEX idx ON v(emb)", None)
    changed = m.update("v", [("emb", [0.0, 0.0])], lambda mm, t, c: c[0] == 9)
    add("UPDATE v SET emb='[0,0]' WHERE id=9", {"affected_rows": changed})
    removed = m.delete("v", lambda mm, t, c: c[0] == 10)
    add("DELETE FROM v WHERE id=10", {"affected_rows": removed})
    add("--restart", None)
    add("--mode ann", None)
    expect = m.select("v", [("col", "id")], None, [(("l2", "emb", q), False)], 4)
    add("SELECT id FROM v ORDER BY L2_DISTANCE(emb,'[0,0]') LIMIT 4", response_rows(["id"], expect))
    return stmts


def case_hard_tx(m):
    m.create("k", [("id", "BIGINT", 0, True), ("tag", "VARCHAR", 8, False),
                   ("vec", "VECTOR", 2, False)])
    stmts = []

    def add(sql, expect=None):
        stmts.append((sql, expect if expect is not None else {}))

    add("CREATE TABLE k (id BIGINT PRIMARY KEY, tag VARCHAR(8), vec VECTOR(2))")
    add("INSERT INTO k VALUES (1,'x','[1,0]'),(2,'x','[0,1]'),(3,'y','[1,1]')",
        {"affected_rows": m.insert("k", [[1, "x", [1.0, 0.0]], [2, "x", [0.0, 1.0]],
                                          [3, "y", [1.0, 1.0]]])})
    add("BEGIN", None)
    m.begin()
    changed = m.update("k", [("vec", [0.0, 0.0])], lambda mm, t, c: c[0] == 1)
    add("UPDATE k SET vec='[0,0]' WHERE id=1", {"affected_rows": changed})
    expect = m.select("k", [("col", "id")], None, [(("l2", "vec", [0.0, 0.0]), False)], 3)
    add("SELECT id FROM k ORDER BY L2_DISTANCE(vec,'[0,0]') LIMIT 3", response_rows(["id"], expect))
    add("ROLLBACK", None)
    m.rollback()
    expect = m.select("k", [("col", "id")], None, [(("l2", "vec", [0.0, 0.0]), False)], 3)
    add("SELECT id FROM k ORDER BY L2_DISTANCE(vec,'[0,0]') LIMIT 3", response_rows(["id"], expect))
    add("BEGIN", None)
    m.begin()
    removed = m.delete("k", lambda mm, t, c: c[0] == 3)
    add("DELETE FROM k WHERE id=3", {"affected_rows": removed})
    add("INSERT INTO k VALUES (1,'dup','[0,0]')", {"error": "duplicate_key"})
    expect = m.select("k", [("col", "id")], None, [("id", False)], None)
    add("SELECT id FROM k ORDER BY id", response_rows(["id"], expect))
    add("COMMIT", None)
    m.commit()
    expect = m.select("k", [("col", "id"), ("col", "tag")], None, [("id", False)], None)
    add("SELECT id, tag FROM k ORDER BY id", response_rows(["id", "tag"], expect))
    add("--restart", None)
    expect = m.select("k", [("col", "id"), ("col", "vec")], None, [("id", False)], None)
    add("SELECT id, vec FROM k ORDER BY id", response_rows(["id", "vec"], expect))
    return stmts


CASES = [
    ("hard_crud", case_hard_crud),
    ("hard_order", case_hard_order),
    ("hard_empty", case_hard_empty),
    ("hard_strings", case_hard_strings),
    ("hard_bigint", case_hard_bigint),
    ("hard_vector", case_hard_vector),
    ("hard_tx", case_hard_tx),
]


def main():
    os.makedirs(OUT, exist_ok=True)
    manifest = {"version": "hard-siblings-v1", "scope": "modelled", "cases": []}
    for name, builder in CASES:
        model = Model()
        stmts = builder(model)
        test_path = "cases/%s.test" % name
        result_path = "cases/%s.result" % name
        os.makedirs(os.path.join(OUT, "cases"), exist_ok=True)
        with open(os.path.join(OUT, test_path), "w", encoding="utf-8", newline="\n") as fh:
            for sql, _ in stmts:
                fh.write(sql + "\n")
        results = [{} if expect is None else expect for sql, expect in stmts
                   if not sql.startswith("--")]
        with open(os.path.join(OUT, result_path), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        manifest["cases"].append({"id": name, "group": "hard", "test": test_path,
                                  "result": result_path,
                                  "statements": len([s for s, _ in stmts
                                                     if not s.startswith("--")])})
    with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    total = sum(c["statements"] for c in manifest["cases"])
    print("wrote %d cases, %d statements to %s" % (len(manifest["cases"]), total, OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
