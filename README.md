# MiniVectorDB

A small, single-process vector database: SQL subset, persistent storage, transactions and crash
recovery. Written from scratch in C++17 (no external dependencies beyond the C++ standard library).

This branch is the submission for the 2026 OceanBase contest extra round; see `DESIGN.md` for the
design and the known limitations.

## Build

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"
```

The `Dockerfile` performs the same build in a builder stage and copies only the binary into the
runtime image.

## Run

```bash
docker build -t minivectordb:latest .
docker run --rm -i -v minivectordb-data:/data --cpus=2 --memory=2g minivectordb:latest
```

or directly:

```bash
./build/minivectordb --data-dir /data
```

The program runs in the foreground, reads SQL from **stdin**, writes results to **stdout** and logs to
**stderr**. It does not open a network listener. `--data-dir` defaults to `/data`.

## Supported SQL

| Statement | Notes |
|---|---|
| `CREATE TABLE t (id BIGINT PRIMARY KEY, s VARCHAR(n), v VECTOR(m), ...)` | one BIGINT primary key, at most one VECTOR column |
| `CREATE VECTOR INDEX name ON t(v)` | index maintenance keeps results correct after DML |
| `INSERT INTO t [(cols)] VALUES (...), (...)` | multi-row, all columns must be provided |
| `SELECT [*｜expr [AS alias], ...] FROM t [WHERE ...] [ORDER BY ... [ASC|DESC]] [LIMIT n]` | `=`, `<`, `<=`, `>`, `>=`, `!=`, `<>`, `AND` |
| `SELECT ... ORDER BY L2_DISTANCE(v, '[1,2,3]') LIMIT k` | exact TopK; filtering happens before TopK |
| `UPDATE t SET col = expr [, ...] [WHERE ...]` | primary key updates are rejected |
| `DELETE FROM t [WHERE ...]` | |
| `BEGIN` / `COMMIT` / `ROLLBACK` | one explicit transaction at a time |

Semantics worth noting:
* `VARCHAR(n)` counts Unicode code points; string comparison is by code point order (UTF-8 byte order).
* `VECTOR(m)` is fixed-dimension float32; `NaN`, `Infinity`, wrong dimensions and values that do not
  fit in float32 are rejected.
* Distances are computed from the stored values. Equal distances order by primary key ascending;
  an `ORDER BY` tie also falls back to primary key ascending.
* Without an explicit `BEGIN`, every successful modifying statement commits on its own.
* Unsupported statements return an error instead of being silently reinterpreted.

## Output format

**Provisional.** The official reader (test driver) that defines statement framing, result separators,
error codes, readiness marker and the `exact`/`ann` switch is published with the task package. Until
then this build uses:

```
READY                       on startup
OK <affected_rows>          a successful DDL/DML statement and the rows it changed
<header row>                a query result: column names, tab separated
<data rows>                 tab separated
END                         end of the query result
ERROR <code> <text>         a failed statement
BYE                         on shutdown
```

`src/protocol/writer.cpp` is the only place that knows this format, so adapting to the official
protocol is a local change.

## Data directory

Everything lives in one directory (`/data` by default):

| File | Purpose |
|---|---|
| `catalog.pages` | table definitions and the vector index marker |
| `t_<name>.pages` | one slotted-page file per table |
| `data.wal` | length-prefixed write-ahead log: committed statements are replayed on startup |

A clean shutdown rolls back an open transaction and checkpoints. After a hard kill the next start
replays committed log records only, so acknowledged commits survive and unfinished transactions never
take effect partially. A process that exits with an open transaction aborts it.

## Tests

```bash
bash tests/run_smoke.sh          # SQL subset, TopK, transactions, restart persistence
bash tests/crash_test.sh         # SIGKILL: committed survives, uncommitted disappears
bash tests/debug_wal.sh          # dumps the WAL and shows what recovery replays
python3 tests/run_public_cases.py   # replays the published mysqltest-style cases
python3 tests/diff_fuzz.py          # random SQL against a reference model
python3 tests/diff_fuzz_state.py    # transactions, restarts and index upkeep against the model
python3 tests/durability_test.py    # restart persistence, one statement shape at a time
python3 tests/probe_syntax.py       # statement shapes a case may use, checking none is rejected
```

`tests/run_public_cases.py` follows the published case README (a fresh data directory per case, one
statement per line, exact `columns`/`affected_rows`, `1e-5` float tolerance, multiset comparison when a
result is marked `unordered`). Point `MVDB_BIN` at `tests/docker_wrapper.sh` to drive the packaged
image instead of a local build.

These are development checks, not the official conformance suite.

## Agent prompt material

`agent/conversation.json` holds the learning material for the extra round's prompt evaluation
(a copy is kept at the repository root because the skeleton shipped an empty `conversation.json`
there and the published description names `agent/conversation.json`).
