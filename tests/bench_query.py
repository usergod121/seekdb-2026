#!/usr/bin/env python3
"""Repeats the graded query shape enough times to compare configurations despite a noisy machine.

Reports the median and the 10th percentile of per-query wall time over many repetitions, so a single
slow sample cannot decide which build looks faster.
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scale_check import Driver, gen  # noqa: E402

ROWS = 10000
DIMS = 128


def bench(label, binary, reps=400):
    created, batches, vectors, probes = gen(ROWS, 40, DIMS)
    tmp = tempfile.mkdtemp(prefix="mvdb_bench_")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir)
    driver = Driver([binary, "--data-dir", data_dir])
    try:
        driver.send(created)
        driver.flush()
        driver.read_ack()
        for batch in batches:
            driver.send("BEGIN;")
            driver.send("INSERT INTO docs VALUES " + ",".join(batch) + ";")
            driver.send("COMMIT;")
            driver.flush()
            for _ in range(3):
                driver.read_ack()

        # warm up
        for sql, _, _ in probes[:20]:
            driver.send(sql)
            driver.flush()
            driver.read_block()

        samples = {"unfiltered": [], "filtered": []}
        for index in range(reps):
            sql, _, category = probes[index % len(probes)]
            start = time.perf_counter()
            driver.send(sql)
            driver.flush()
            driver.read_block()
            samples["filtered" if category is not None else "unfiltered"].append(
                time.perf_counter() - start)

        line = [label]
        for shape in ("unfiltered", "filtered"):
            values = sorted(samples[shape])
            median = values[len(values) // 2] * 1e6
            p10 = values[len(values) // 10] * 1e6
            line.append("%s median %6.1f p10 %6.1f" % (shape, median, p10))
        print(" | ".join(line))
    finally:
        driver.close()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    binary = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    for worker_env in ("1", "2", "4"):
        os.environ["MVDB_SCAN_WORKERS"] = worker_env
        for _ in range(rounds):
            bench("workers=%s" % worker_env, binary)


if __name__ == "__main__":
    main()
