#!/usr/bin/env bash
# Repeats the write load a few times and reports the minimum of each profiled stage, because the VM's
# scheduling noise is larger than the differences being measured.
set -u
cd /mnt/e/mvdb || exit 1
RUNS=${RUNS:-3}
BATCHES=${BATCHES:-100}
rm -f /tmp/prof_runs.txt
for i in $(seq 1 "$RUNS"); do
  python3 -u tests/profile_write.py --batches "$BATCHES" 2>&1 | sed -n 's/^\[profile\] //p' >> /tmp/prof_runs.txt
done
python3 - "$RUNS" <<'PY'
import collections, sys
runs = int(sys.argv[1])
best = {}
for line in open('/tmp/prof_runs.txt'):
    parts = line.split()
    if len(parts) < 4:
        continue
    stage = parts[0]
    stage = parts[0]
    total = float(parts[1])
    calls = parts[2]
    if stage not in best or total < best[stage][0]:
        best[stage] = (total, calls, parts[3])
print("stage              best_total_ms   calls   us/call")
for stage, (total, calls, per) in sorted(best.items(), key=lambda kv: -kv[1][0]):
    print("%-18s %13.3f %7s %8s" % (stage, total, calls, per))
PY
