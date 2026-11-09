#!/bin/bash
# Local smoke test. Not the official driver: it just feeds a SQL script to the binary and shows the
# result stream, then restarts on the same data directory to prove persistence.
set -u
BIN=${BIN:-build/minivectordb}
DIR=${1:-/tmp/mvdb_smoke}
SQL=${2:-tests/smoke.sql}
rm -rf "$DIR"
echo "=== run 1 (fresh data dir: $DIR)"
"$BIN" --data-dir "$DIR" < "$SQL"
echo "=== run 2 (restart on the same data dir; committed rows must still be there)"
printf "SELECT id, title, category FROM docs ORDER BY id;\n" | "$BIN" --data-dir "$DIR"
echo "=== files in the data dir:"
ls -l "$DIR"
