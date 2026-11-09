#!/bin/bash
# Crash recovery check: a committed row must survive SIGKILL, an uncommitted one must not appear.
set -u
BIN=${BIN:-build/minivectordb}
DIR=${1:-/tmp/mvdb_crash}
FIFO=/tmp/mvdb_crash.fifo
rm -rf "$DIR" 
rm -f "$FIFO"
mkfifo "$FIFO"

echo "=== phase 1: committed row + open transaction, then SIGKILL"
"$BIN" --data-dir "$DIR" < "$FIFO" > /tmp/mvdb_crash.out 2>/tmp/mvdb_crash.err &
PID=$!
exec 3>"$FIFO"

send() { printf '%s\n' "$1" >&3; sleep 0.2; }

send "CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(8));"
send "INSERT INTO t VALUES (1,'keep');"          # autocommit: must survive
send "BEGIN;"
send "INSERT INTO t VALUES (2,'drop');"          # uncommitted: must not survive
sleep 0.3
kill -9 "$PID" 2>/dev/null
wait "$PID" 2>/dev/null
exec 3>&-
echo "--- output captured before the kill:"
cat /tmp/mvdb_crash.out
echo "--- stderr:"
cat /tmp/mvdb_crash.err

echo "=== phase 2: restart on the same data dir"
printf "SELECT id, v FROM t ORDER BY id;\n" | "$BIN" --data-dir "$DIR" 2>/tmp/mvdb_crash2.err
echo "--- stderr:"
cat /tmp/mvdb_crash2.err

echo "=== phase 3: commit then SIGKILL immediately after the acknowledged COMMIT"
DIR2=/tmp/mvdb_crash_commit
rm -rf "$DIR2"
mkfifo "$FIFO"
"$BIN" --data-dir "$DIR2" < "$FIFO" > /tmp/mvdb_crash3.out 2>/tmp/mvdb_crash3.err &
PID=$!
exec 3>"$FIFO"
send "CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(8));"
send "BEGIN;"
send "INSERT INTO t VALUES (7,'kept');"
send "COMMIT;"
sleep 0.3
kill -9 "$PID" 2>/dev/null
wait "$PID" 2>/dev/null
exec 3>&-
echo "--- output captured before the kill:"
cat /tmp/mvdb_crash3.out
echo "--- after restart (row 7 must be present):"
printf "SELECT id, v FROM t ORDER BY id;\n" | "$BIN" --data-dir "$DIR2"
rm -f "$FIFO"
