#!/usr/bin/env python3
"""Generates the shape of the graded workload: N rows with a 128 dimension vector, a category column
and a payload string, plus M TopK queries (half of them filtered). Used by tests/perf_smoke.sh."""
import random
import sys

rows = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
queries = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
dims = int(sys.argv[3]) if len(sys.argv) > 3 else 128
batch = 100
rng = random.Random(20261109)

print("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(16), title VARCHAR(64), emb VECTOR(%d));" % dims)
ids = list(range(1, rows + 1))
rows_sql = []
for i in ids:
    vec = ",".join("%.4f" % rng.uniform(-1.0, 1.0) for _ in range(dims))
    rows_sql.append("(%d,'cat%d','doc %d','[%s]')" % (i, i % 8, i, vec))

for start in range(0, len(rows_sql), batch):
    print("BEGIN;")
    print("INSERT INTO docs VALUES " + ",".join(rows_sql[start:start + batch]) + ";")
    print("COMMIT;")

for q in range(queries):
    vec = ",".join("%.4f" % rng.uniform(-1.0, 1.0) for _ in range(dims))
    if q % 2 == 0:
        print("SELECT id, L2_DISTANCE(emb,'[%s]') AS d FROM docs ORDER BY L2_DISTANCE(emb,'[%s]') ASC LIMIT 10;" % (vec, vec))
    else:
        print("SELECT id, L2_DISTANCE(emb,'[%s]') AS d FROM docs WHERE category = 'cat%d' ORDER BY L2_DISTANCE(emb,'[%s]') ASC LIMIT 10;" % (vec, q % 8, vec))
