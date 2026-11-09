#!/usr/bin/env bash
set -u
cd /mnt/e/mvdb || exit 1
git add -A src tests
git -c user.name=usergod121 -c user.email=usergod121@users.noreply.github.com commit -q -m "Report affected rows, resolve ORDER BY aliases, fix grew-row duplicates

The public mysqltest cases pin three behaviours the engine did not have yet:

- INSERT/UPDATE/DELETE must report the exact affected row count, so the result
  carries it and the writer prints it.
- ORDER BY may name a projection alias (ORDER BY distance for
  L2_DISTANCE(embedding, ...) AS distance); each order item is now mapped to the
  expression it names before the scan, so the fast path still applies.
- Heap::update on a grown row appended the new version and left the old slot
  live, so an UPDATE that enlarged a value made the row appear twice. The insert
  now happens first and the old slot is freed afterwards, which also means a
  failed insert can no longer destroy the live row.

A .test file may begin with a UTF-8 BOM, which is not part of the first
statement; the splitter strips it. tests/run_public_cases.py replays the public
cases the way their README describes (fresh data directory per case, one
statement per line, exact columns/affected_rows, 1e-5 float tolerance, multiset
comparison when unordered): 32/32 statements pass."
git log --oneline -1
