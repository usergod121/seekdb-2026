#!/bin/bash
# Debug the durability path: feed statements, SIGKILL, then dump the WAL the way recovery sees it.
set -u
BIN=${BIN:-build/minivectordb}
DIR=/tmp/mvdb_dbg
FIFO=/tmp/mvdb_dbg.fifo
rm -rf "$DIR" "$FIFO"
mkfifo "$FIFO"
"$BIN" --data-dir "$DIR" < "$FIFO" > /tmp/mvdb_dbg.out 2>/tmp/mvdb_dbg.err &
PID=$!
exec 3>"$FIFO"
send() { printf '%s\n' "$1" >&3; sleep 0.25; }
send "CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(8));"
send "INSERT INTO t VALUES (1,'keep');"
sleep 0.5
echo "--- in-memory check before the kill (must show row 1):"
send "SELECT id, v FROM t ORDER BY id;"
sleep 0.3
kill -9 "$PID" 2>/dev/null
wait "$PID" 2>/dev/null
exec 3>&-
echo "--- process output:"
cat /tmp/mvdb_dbg.out
echo "--- stderr:"
cat /tmp/mvdb_dbg.err
echo "--- data dir:"
ls -l "$DIR"
echo "--- WAL (cat -A shows line ends):"
cat -A "$DIR/data.wal" 2>/dev/null | head -20
echo "--- restart:"
printf "SELECT id, v FROM t ORDER BY id;\n" | "$BIN" --data-dir "$DIR"
rm -f "$FIFO"
