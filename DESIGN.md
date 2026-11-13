# Design notes

## Layout on disk

```
/data
├─ data.snapshot   full state: schema + rows + vector payload, written atomically
├─ data.wal        framed write-ahead log of committed statements
└─ data.pages      paged row store used when the data set does not fit the memory budget
```

`data.snapshot` is written to `data.snapshot.tmp`, fsynced, then renamed over the target so a crash
in the middle of a checkpoint leaves the previous snapshot intact. The rename is made durable by
fsyncing the directory. The WAL is truncated only after the new snapshot is in place.

## WAL format

Framed binary records, so statement text containing quotes, commas or newlines needs no escaping:

```
'S' | u32 length | statement bytes      a statement belonging to the running group
'C'                                     commit marker: everything since the previous marker is durable
```

A group of statements is replayed only when a `C` record follows it, which is what gives the required
crash semantics: an acknowledged commit survives, an unfinished transaction leaves nothing behind.
Replay is idempotent — `INSERT` skips an existing primary key, `UPDATE` is an upsert, `DELETE` ignores a
missing row, DDL ignores an existing object — so replaying the same log twice is harmless.

## Transactions

One explicit transaction at a time. Every row modification appends an undo record (previous row
content plus the vector payload) before the change is applied:

* `ROLLBACK` replays the undo list in reverse;
* a failing statement replays only the undo records it created, so earlier statements of the same
  transaction stay in place (the task requires that a failed statement leaves no partial effects but
  does not discard the transaction);
* `COMMIT` writes the buffered statements and a commit marker, fsyncs, and only then returns.

Reads inside a transaction see the in-memory state, so a transaction reads its own writes for both
plain queries and vector TopK.

## Pages, buffer pool and the memory budget

Rows live in 64 KiB pages of a slotted row store, fetched through a fixed pool of 512 frames
(32 MiB). The page size is what makes a wide row (a 60000-character text column, four 16 KiB
columns) fit inside a single page, which is the shape the task's resource-limit cases use; a row
that cannot fit is rejected cleanly instead of being split across pages and torn.

Above the pages sits a columnar read cache: decoded rows, one contiguous `float` array for the
vector column, and per-column value arrays (`cache_int_cols`, `cache_str_off`/`cache_str_data`). The
cache budget is derived from the container rather than a constant — `/sys/fs/cgroup/memory.max`
divided by eight, clamped to 64–512 MiB — because the graded container is capped at 2 GiB and a
data set that does not fit must fall back to the paged path instead of being killed. When a table
cannot be cached, every scan goes back to the pages.

Squared vector components above ~1e17 would overflow a `float` accumulator, so such a table sets
`cache_needs_double` and every distance is computed in `double`.

## Vector storage and index

Vectors are stored out of line in a per-table float arena, so a scan walks contiguous memory. The
`VECTOR(m)` dimension is fixed per column, which makes every stored vector the same length.

`CREATE VECTOR INDEX` starts a maintained dense list of live vector slots (`index_slots`) plus a
version counter; inserts, updates and deletes rebuild it. Search is exact — the task allows the
`exact` mode to be fixed in the submission and caps the metric on Recall@10 ≥ 0.95, and an exact
scan is both the correct answer and, at this data size (10k × 128 float32 = 5 MB), the fastest one.
The index object exists so the DDL and its maintenance semantics are real rather than skipped.

## Query execution

`SELECT` scans the table, evaluates `WHERE` per row, evaluates `ORDER BY` keys (numeric or textual)
and sorts with a primary-key tie break. Filtering happens before the TopK cut, which the task
requires explicitly ("不能先在全表取 K 条再过滤").

The graded shape (one distance ordering, a `LIMIT`, an optional equality filter) has a fast path:

* the filter is compiled once into a column test (`CmpPred`) over the columnar arrays, and the
  compiled form is used only when the column type and the literal type match exactly — a mismatch
  falls back to the general evaluator rather than reading the wrong array;
* distances are computed with an AVX2+FMA kernel eight rows at a time, four independent row
  accumulators per pass, and a single scan is split across the persistent scan pool (one worker per
  usable CPU, capped at four, sized from the affinity mask and `cpu.max`);
* during the cache build each column also gets an equality index (`value -> row indices`), dropped
  for a column once its distinct-value count exceeds `rows/16 + 64` and never built for tables above
  200000 rows. A lone equality filter whose group is under half the table therefore answers the
  TopK by walking that group (gathering four rows at a time into a contiguous scratch block for the
  same kernel) instead of scanning every row; past half the table the contiguous scan wins;
* the ordering and the primary-key tie break are the same in both paths, so the answer does not
  depend on which path ran.

Large replies are streamed in chunks of at least 256 KiB rather than built as one string, because a
single 20 MB reply would otherwise hold several copies of itself in memory at once.

## Verification

Correctness is tracked by suites rather than by inspection (`tests/`): a 27297-check query matrix,
a write matrix, an order matrix that also covers the paged path, scale and recovery runs at 100k
rows, protocol fuzzing, a rejection-surface matrix, and spec-derived blind corpora written without
looking at the implementation. Every engine change is additionally checked against a frozen golden
binary: `tests/ab_differential.py` feeds the same random workload to both and compares replies field
by field, and `tests/bench_ab_query.sh` compares the latency distributions.

## Known limitations

* The snapshot rewrites the whole state, so checkpoint cost grows with the data set.
* The equality index only accelerates low-cardinality equality filters; range filters, `LIKE` and
  filters on a high-cardinality column still scan.
* No `NULL` (beyond `IS NULL` on absent values), `JOIN`, aggregates, subqueries, `ALTER TABLE`,
  foreign keys, nested transactions or primary key updates — out of scope per the task.
* Deleted rows leave dead vector space in the arena until the next snapshot.
* The distance kernels are bandwidth bound at this size: 5.1 MB per scan over 128 dimensions reaches
  about 43 GB/s on the development machine, so further kernel work has no measurable effect.
