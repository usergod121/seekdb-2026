# Design notes

## Layout on disk

```
/data
├─ data.snapshot   full state: schema + rows + vector payload, written atomically
└─ data.wal        framed write-ahead log of committed statements
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

## Vector storage and index

Vectors are stored out of line in a per-table float arena, so a scan walks contiguous memory. The
`VECTOR(m)` dimension is fixed per column, which makes every stored vector the same length.

`CREATE VECTOR INDEX` starts a maintained dense list of live vector slots (`index_slots`) plus a
version counter; inserts, updates and deletes rebuild it. Search is exact — the task caps the metric
on Recall@10 ≥ 0.95 and allows the `exact` mode to be fixed in the submission, and an exact scan is
both the correct answer and, at this data size (10k × 128 float32 = 5 MB), the fastest one. The index
object exists so the DDL and its maintenance semantics are real rather than skipped.

## Query execution

`SELECT` scans the table, evaluates `WHERE` per row, evaluates `ORDER BY` keys (numeric or textual)
and sorts with a primary-key tie break. Filtering happens before the TopK cut, which the task requires
explicitly ("不能先在全表取 K 条再过滤").

## Known limitations

* No page-based buffer pool yet: rows are kept in memory, so a data set larger than the memory budget
  would not fit. The task's resource-limit cases require a paged store; that is the next milestone.
* The snapshot rewrites the whole state, so checkpoint cost grows with the data set.
* No `NULL`, `JOIN`, aggregates, subqueries, `ALTER TABLE`, foreign keys, nested transactions or
  primary key updates — all out of scope per the task.
* Deleted rows leave dead vector space in the arena until the next snapshot.
* The output format and error codes are provisional until the task package publishes the official
  driver protocol.
