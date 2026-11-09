#!/usr/bin/env bash
# Shows when the engine writes the page file: pwrite/fsync traffic around a small load.
set -u
cd /mnt/e/mvdb || exit 1
rm -rf /tmp/w_trace && mkdir -p /tmp/w_trace
{
  echo "CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(4));"
  for b in 0 1 2; do
    echo "BEGIN;"
    rows=""
    for i in $(seq 1 40); do
      pk=$((b * 40 + i))
      [ -n "$rows" ] && rows="$rows,"
      rows="$rows($pk,'[1.0,2.0,3.0,4.0]')"
    done
    echo "INSERT INTO t VALUES $rows;"
    echo "COMMIT;"
  done
  echo "SELECT id FROM t LIMIT 1;"
} > /tmp/w_trace/load.sql

strace -f -e trace=pwrite64,write,fsync,fdatasync,ftruncate -o /tmp/w_trace/trace.txt \
  ./build/minivectordb --data-dir /tmp/w_trace/data < /tmp/w_trace/load.sql > /dev/null 2>&1

echo "== writes to t_t.pages"
grep -c 'pwrite64' /tmp/w_trace/trace.txt || true
awk '/pwrite64/ {print}' /tmp/w_trace/trace.txt | head -10
echo "== fsync/fdatasync lines"
grep -c -e fsync -e fdatasync /tmp/w_trace/trace.txt || true
echo "== any pwrite before the first fdatasync?"
awk '/pwrite64|fdatasync|fsync/ {print; n++} n>=12 {exit}' /tmp/w_trace/trace.txt
rm -rf /tmp/w_trace
