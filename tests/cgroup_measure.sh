#!/bin/bash
# Measures the graded quantities the way the platform does: the process runs inside a private cgroup v2,
# T is wall clock, B is the cgroup's io.stat rbytes+wbytes delta (physical bytes on the data device) and
# M is memory.peak, which - unlike RSS - also covers the file page cache the process populated.
set -u
BIN=${BIN:-build/minivectordb}
DIR=${1:-/tmp/mvdb_cg}
ROWS=${ROWS:-10000}
QUERIES=${QUERIES:-1000}
SQL=/tmp/mvdb_cg.sql
CG=/sys/fs/cgroup/mvdb_measure

if [ ! -s "$SQL" ]; then
  python3 tests/perf_gen.py "$ROWS" "$QUERIES" > "$SQL"
fi
awk '/^SELECT/{exit} {print}' "$SQL" > /tmp/mvdb_cg_load.sql
awk '/^SELECT/{print}' "$SQL" > /tmp/mvdb_cg_query.sql
rm -rf "$DIR"

# private cgroup; memory and io controllers must be usable
if [ -d "$CG" ]; then
  rmdir "$CG" 2>/dev/null || {
    while read -r p; do kill -9 "$p" 2>/dev/null; done < "$CG/cgroup.procs"
    rmdir "$CG" 2>/dev/null
  }
fi
mkdir -p "$CG" || { echo "cannot create cgroup"; exit 1; }
echo "+memory +io" > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true

io_totals() { awk -F'[ =]' '/rbytes=/{r+=$3; w+=$7} END{printf "%d %d", r+0, w+0}' "$CG/io.stat" 2>/dev/null || echo "0 0"; }

run_phase() { # run_phase <label> <sqlfile>
  local label=$1 file=$2
  local before after t0 t1 m
  before=$(io_totals)
  t0=$(date +%s%N)
  "$BIN" --data-dir "$DIR" < "$file" > /tmp/mvdb_cg_$label.out 2>/tmp/mvdb_cg_$label.err &
  local pid=$!
  printf '%s' "$pid" > "$CG/cgroup.procs" 2>/dev/null
  wait "$pid"
  t1=$(date +%s%N)
  after=$(io_totals)
  m=$(cat "$CG/memory.peak" 2>/dev/null || echo 0)
  local ms=$(( (t1 - t0) / 1000000 ))
  local rb=$(( ${after%% *} - ${before%% *} ))
  local wb=$(( ${after##* } - ${before##* } ))
  printf '%-6s T=%d ms  B_read=%d KiB  B_write=%d KiB  M_peak=%d KiB\n' \
    "$label" "$ms" $(( rb / 1024 )) $(( wb / 1024 )) $(( m / 1024 ))
  printf '%s' "$ms" > /tmp/mvdb_cg_${label}_ms
}

echo "cgroup: $CG  (controllers: $(cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null))"
run_phase load  /tmp/mvdb_cg_load.sql
# a fresh cgroup per phase keeps memory.peak meaningful for the query phase alone
echo 0 > "$CG/memory.peak" 2>/dev/null || true
run_phase query /tmp/mvdb_cg_query.sql
printf 'data: '; ls -l "$DIR" 2>/dev/null | awk 'NR>1 {s+=$5} END {printf "%.1f MiB\n", s/1048576}'
printf 'errors: %s\n' "$(cat /tmp/mvdb_cg_load.err /tmp/mvdb_cg_query.err | grep -c ERROR || true)"
rmdir "$CG" 2>/dev/null || true
