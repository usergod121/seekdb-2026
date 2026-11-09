#!/usr/bin/env bash
# Reproduces the crash scenario under strace and shows every write to the table page file.
set -u
cd /mnt/e/mvdb || exit 1
rm -rf /tmp/w_crash && mkdir -p /tmp/w_crash/data
FIFO=/tmp/w_crash/in
mkfifo "$FIFO"

strace -f -e trace=pwrite64,ftruncate,fsync,fdatasync -o /tmp/w_crash/trace.txt \
  ./build/minivectordb --data-dir /tmp/w_crash/data < "$FIFO" > /tmp/w_crash/out.txt 2>&1 &
ENGINE=$!
exec 9> "$FIFO"

send() { printf '%s\n' "$1" >&9; sleep 0.15; }

send "CREATE TABLE docs (id BIGINT PRIMARY KEY, emb VECTOR(4));"
for b in 1 2 3 4 5 6; do
  rows=""
  for i in $(seq 1 100); do
    pk=$((b * 100 + i))
    [ -n "$rows" ] && rows="$rows,"
    rows="$rows($pk,'[1.0,2.0,3.0,4.0]')"
  done
  send "BEGIN;"
  send "INSERT INTO docs VALUES $rows;"
  send "COMMIT;"
done

echo "== page file before the kill:"
ls -l /tmp/w_crash/data/
od -An -tx1 -N 16 /tmp/w_crash/data/t_docs.pages 2>/dev/null || echo "(no page file)"

kill -9 "$ENGINE" 2>/dev/null
sleep 0.3
exec 9>&-
echo "== writes recorded by strace:"
grep -e pwrite64 -e ftruncate /tmp/w_crash/trace.txt | head -12
echo "== after the kill:"
ls -l /tmp/w_crash/data/
od -An -tx1 -N 16 /tmp/w_crash/data/t_docs.pages 2>/dev/null || echo "(no page file)"
