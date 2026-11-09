#!/usr/bin/env python3
"""Differential test for the stateful parts of the gate: transactions, restart persistence, index upkeep.

Same reference model as diff_fuzz.py, but the workload opens transactions (and either commits or rolls
back), creates the vector index while data is present, and restarts the process on the same data
directory. After every restart and every transaction boundary the whole table is compared with the
model, so a lost row, a resurrected row, or an index that disagrees with the data shows up immediately.
"""
import argparse
import copy
import os
import random
import shutil
import sys
import tempfile

from diff_fuzz import CATEGORIES, CONTENTS, Model, Session, f32, l2, vec_lit

SETUP = ("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), content VARCHAR(16), "
         "embedding VECTOR(2));")

TRACE = False


def trace(text):
    if TRACE:
        print("    " + text)


def check_full_table(session, model, where):
    result = session.run("SELECT * FROM docs;")
    trace("%s | SELECT * FROM docs;" % where)
    if result["kind"] != "rows":
        return ["%s: expected rows, got %s" % (where, result["kind"])]
    want = model.select(["id", "category", "content", "embedding"], lambda r: True, None, None)
    got = result["rows"]
    problems = []
    if len(want) != len(got):
        problems.append("%s: %d rows != %d" % (where, len(got), len(want)))
    remaining = list(got)
    for row in want:
        target = [row[0], row[1], row[2], row[3]]
        for index, candidate in enumerate(remaining):
            if len(candidate) != 4:
                continue
            if (int(candidate[0]) == target[0] and candidate[1] == target[1]
                    and candidate[2] == target[2]):
                vec = [float(c) for c in candidate[3].strip("[]").split(",")]
                if len(vec) == 2 and all(abs(a - b) <= 1e-5 for a, b in zip(vec, target[3])):
                    del remaining[index]
                    break
        else:
            problems.append("%s: missing row %r" % (where, target))
    if remaining:
        problems.append("%s: unexpected rows %r" % (where, remaining))
    if problems:
        trace("%s: got %r" % (where, got))
        trace("%s: want %r" % (where, want))
    return problems


def check_topk(session, model, rng):
    q = [f32(rng.choice([0, 1, 2, -1, 0.5])), f32(rng.choice([0, 1, 2, 3]))]
    k = rng.choice([1, 2, 3, 5, 10])
    sql = ("SELECT id, L2_DISTANCE(embedding, '%s') AS distance FROM docs ORDER BY distance LIMIT %d;"
           % (vec_lit(q), k))
    result = session.run(sql)
    trace("topk | " + sql)
    if result["kind"] != "rows":
        return ["topk: expected rows, got %s" % result["kind"]]
    scored = sorted(
        ((l2(row["embedding"], [f32(c) for c in q]), row["id"]) for row in model.rows.values()))
    want = [(pk, dist) for dist, pk in scored[:k]]
    got = [(int(row[0]), float(row[1])) for row in result["rows"]]
    problems = []
    if [pk for pk, _ in want] != [pk for pk, _ in got]:
        problems.append("topk ids %r != %r (query %r)" % (got, want, q))
    else:
        for (pk, dist), (_, have) in zip(want, got):
            if abs(dist - have) > 1e-5:
                problems.append("topk distance for %d: %r != %r" % (pk, have, dist))
    return problems


def main():
    global TRACE
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--steps", type=int, default=60)
    ap.add_argument("--trace", action="store_true")
    args = ap.parse_args()
    TRACE = args.trace

    failures = 0
    statements = 0
    for seed in range(args.seeds):
        rng = random.Random(1000 + seed)
        tmp = tempfile.mkdtemp(prefix="mvdb_state_")
        data_dir = os.path.join(tmp, "data")
        os.makedirs(data_dir)
        session = Session(data_dir)
        model = Model()
        snapshot = None
        in_tx = False
        index_created = False
        problems = []
        if TRACE:
            print("---- seed %d" % seed)
        try:
            session.run(SETUP)
            trace("setup | " + SETUP)
            for step in range(args.steps):
                statements += 1
                roll = rng.random()
                mark = "seed %d step %d" % (seed, step)
                if roll < 0.08 and not in_tx:
                    session.run("BEGIN;")
                    trace(mark + " | BEGIN;")
                    in_tx = True
                    snapshot = copy.deepcopy(model.rows)
                    continue
                if roll < 0.16 and in_tx:
                    if rng.random() < 0.5:
                        session.run("COMMIT;")
                        trace(mark + " | COMMIT;")
                        snapshot = None
                    else:
                        session.run("ROLLBACK;")
                        trace(mark + " | ROLLBACK;")
                        model.rows = snapshot
                        snapshot = None
                    in_tx = False
                    problems.extend(check_full_table(session, model, mark + " after tx"))
                    continue
                if roll < 0.20 and not index_created:
                    result = session.run("CREATE VECTOR INDEX idx_emb ON docs (embedding);")
                    trace(mark + " | CREATE VECTOR INDEX")
                    if result["kind"] == "error":
                        problems.append("%s: create index: %s" % (mark, result["error"]))
                    index_created = True
                    continue
                if roll < 0.24:
                    problems.extend(check_topk(session, model, rng))
                    continue
                if roll < 0.24:
                    problems.extend(check_topk(session, model, rng))
                    continue
                if roll < 0.27:
                    session.close()
                    session = Session(data_dir)
                    if in_tx:
                        # A process that goes away with an open transaction aborts it, so the reference
                        # model has to abort it as well before the states are compared.
                        trace(mark + " | restart (open transaction aborted)")
                        model.rows = snapshot
                        snapshot = None
                        in_tx = False
                    else:
                        trace(mark + " | restart")
                    problems.extend(check_full_table(session, model, mark + " after restart"))
                    continue
                if roll < 0.55:
                    free = [pk for pk in range(1, 13) if pk not in model.rows]
                    if not free:
                        continue
                    pk = rng.choice(free)
                    row = {
                        "id": pk,
                        "category": rng.choice(CATEGORIES),
                        "content": rng.choice(CONTENTS),
                        "embedding": [f32(rng.choice([0, 1, 2, 3])), f32(rng.choice([0, 1, 2]))],
                    }
                    sql = "INSERT INTO docs VALUES (%d, '%s', '%s', '%s');" % (
                        row["id"], row["category"], row["content"], vec_lit(row["embedding"]))
                    result = session.run(sql)
                    trace(mark + " | " + sql)
                    if result["kind"] != "ok" or result["affected"] != 1:
                        problems.append("%s: insert %s -> %s %r" % (
                            mark, sql, result["kind"], result.get("affected")))
                    else:
                        model.insert(row)
                    continue
                if roll < 0.75:
                    if not model.rows:
                        continue
                    pk = rng.choice(sorted(model.rows))
                    if rng.random() < 0.5:
                        value = rng.choice(CONTENTS)
                        sql = "UPDATE docs SET content = '%s' WHERE id = %d;" % (value, pk)
                        change = ("content", value)
                    else:
                        vec = [f32(rng.choice([0, 1, 2, 3])), f32(rng.choice([0, 1, 2]))]
                        sql = "UPDATE docs SET embedding = '%s' WHERE id = %d;" % (vec_lit(vec), pk)
                        change = ("embedding", vec)
                    result = session.run(sql)
                    trace(mark + " | " + sql)
                    if result["kind"] != "ok" or result["affected"] != 1:
                        problems.append("%s: %s -> %s %r" % (
                            mark, sql, result["kind"], result.get("affected")))
                    else:
                        model.rows[pk][change[0]] = change[1]
                    continue
                if roll < 0.9:
                    if not model.rows:
                        continue
                    pk = rng.choice(sorted(model.rows))
                    sql = "DELETE FROM docs WHERE id = %d;" % pk
                    result = session.run(sql)
                    trace(mark + " | " + sql)
                    if result["kind"] != "ok" or result["affected"] != 1:
                        problems.append("%s: delete %d -> %s %r" % (
                            mark, pk, result["kind"], result.get("affected")))
                    else:
                        del model.rows[pk]
                    continue
                problems.extend(check_topk(session, model, rng))
                if len(problems) > 8:
                    break
        finally:
            session.close()

        if in_tx:
            # The engine aborts an open transaction when the process exits, so the model does too.
            model.rows = snapshot
            snapshot = None
            in_tx = False
        session = Session(data_dir)
        try:
            problems.extend(check_full_table(session, model, "seed %d final" % seed))
        finally:
            session.close()
        shutil.rmtree(tmp, ignore_errors=True)
        failures += len(problems)
        for problem in problems:
            print(problem)

    print()
    print("%d workload step(s), %d problem(s)" % (statements, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
