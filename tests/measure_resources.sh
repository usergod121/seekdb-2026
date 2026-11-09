#!/bin/bash
# Resource accounting for the graded workload, in the shape the task defines:
#   T = wall clock, B = physical bytes read+written, M = peak memory.
# B comes from /proc/<pid>/io (read_bytes + write_bytes are the physical ones; rchar/wchar would count
# page-cache hits) and M from VmHWM (peak RSS). The page cache the process itself populated is not in
# RSS, so the data file size is reported as the upper bound of what a cgroup memory.peak would add.
set -u
BIN=${BIN:-build/minivectordb}
DIR=${1:-/tmp/mvdb_res}
ROWS=${ROWS:-10000}
QUERIES=${QUERIES:-1000}
SQL=/tmp/mvdb_res.sql
FIFO=/tmp/mvdb_res.fifo

if [ ! -s "$SQL" ]; then
  python3 tests/perf_gen.py "$ROWS" "$QUERIES" > "$SQL"
fi
awk '/^SELECT/{exit} {print}' "$SQL" > /tmp/mvdb_res_load.sql
awk '/^SELECT/{print}' "$SQL" > /tmp/mvdb_res_query.sql
rm -rf "$DIR"
rm -f "$FIFO"
mkfifo "$FIFO"

"$BIN" --data-dir "$DIR" < "$FIFO" > /tmp/mvdb_res.out 2>/tmp/mvdb_res.err &
PID=$!
exec 3>"$FIFO"

field() { # field <file> <pattern>
  grep -m1 "$2" "$1" 2>/dev/null | awk '{print $2}' || echo 0
}

sample_io() {
  local r w
  r=$(field "/proc/$PID/io" '^read_bytes:')
  w=$(field "/proc/$PID/io" '^write_bytes:')
  echo "${r:-0} ${w:-0}"
}
peak_kb() { field "/proc/$PID/status" '^VmHWM:' | awk '{print $1}'; }

sleep 0.5
read -r IO_R0 IO_W0 <<< "$(sample_io)"

T0=$(date +%s%N)
cat /tmp/mvdb_res_load.sql >&3
# wait for the load to finish: the output ends with one OK line per statement
LOAD_N=$(grep -c . /tmp/mvdb_res_load.sql)
for _ in $(seq 1 600); do
  [ "$(grep -c '^OK$\|^ERROR' /tmp/mvdb_res.out 2>/dev/null)" -ge "$LOAD_N" ] && break
  sleep 0.05
done
T1=$(date +%s%N)
read -r IO_R1 IO_W1 <<< "$(sample_io)"
M1=$(peak_kb)

TQP0=$(date +%s%N)
cat /tmp/mvdb_res_query.sql >&3
Q_N=$(grep -c . /tmp/mvdb_res_query.sql)
for _ in $(seq 1 6000); do
  [ "$(grep -c '^END$' /tmp/mvdb_res.out 2>/dev/null)" -ge "$Q_N" ] && break
  sleep 0.05
done
TQP1=$(date +%s%N)
read -r IO_R2 IO_W2 <<< "$(sample_io)"
M2=$(peak_kb)

exec 3>&-
wait "$PID" 2>/dev/null

LOAD_MS=$(( (T1 - T0) / 1000000 ))
QUERY_MS=$(( (TQP1 - TQP0) / 1000000 ))
printf 'load:   T=%d ms   B_write=%d KiB   B_read=%d KiB\n' \
  "$LOAD_MS" $(( (IO_W1 - IO_W0) / 1024 )) $(( (IO_R1 - IO_R0) / 1024 ))
printf 'query:  T=%d ms   B_write=%d KiB   B_read=%d KiB   (%.0f us/query)\n' \
  "$QUERY_MS" $(( (IO_W2 - IO_W1) / 1024 )) $(( (IO_R2 - IO_R1) / 1024 )) \
  "$(awk -v t="$QUERY_MS" -v q="$QUERIES" 'BEGIN{printf "%.0f", t*1000/q}')"
printf 'M:      peak after load=%d KiB, after query=%d KiB (process RSS)\n' "$M1" "$M2"
printf 'data files: '; ls -l "$DIR" | awk 'NR>1 {s+=$5} END {printf "%.1f MiB total\n", s/1048576}'
printf 'errors: load=%s query=%s\n' \
  "$(grep -c ERROR /tmp/mvdb_res.out || true)" "$(grep -c ERROR /tmp/mvdb_res.err || true)"
rm -f "$FIFO"
