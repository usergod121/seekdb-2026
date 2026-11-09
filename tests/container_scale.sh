#!/usr/bin/env bash
# Runs the graded workload inside the packaged image and reports wall time and the container's peak
# memory (cgroup memory.peak, which includes page cache, exactly the way the metric is defined).
set -u
cd /mnt/e/mvdb || exit 1
IMAGE="${MVDB_IMAGE:-mvdb:check}"
WORK=/tmp/mvdb_scale
rm -rf "$WORK" && mkdir -p "$WORK"
python3 tests/perf_gen.py 10000 1000 128 > "$WORK/workload.sql"
echo "workload: $(wc -c < "$WORK/workload.sql") bytes, $(grep -c ';' "$WORK/workload.sql") statements"

docker run --rm -v "$WORK:/work" --entrypoint sh "$IMAGE" -c '
  mkdir -p /data
  start=$(date +%s%3N)
  /app/minivectordb --data-dir /data < /work/workload.sql > /work/out.txt
  end=$(date +%s%3N)
  echo "wall: $((end - start)) ms"
  echo "memory.peak: $(cat /sys/fs/cgroup/memory.peak) bytes"
  echo "io.stat: $(cat /sys/fs/cgroup/io.stat 2>/dev/null | tr "\n" " ")"
  echo "output lines: $(wc -l < /work/out.txt)"
  du -sk /data | awk "{print \"data dir: \" \$1 \" KiB\"}"
' 2>&1 | tail -6
rm -rf "$WORK"
