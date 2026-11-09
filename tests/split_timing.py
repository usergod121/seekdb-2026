#!/usr/bin/env python3
"""Times the two query shapes of the graded workload separately."""
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scale_check import Driver, gen  # noqa: E402

ROWS = 10000
DIMS = 128


def main():
    created, batches, vectors, probes = gen(ROWS, 200, DIMS)
    tmp = tempfile.mkdtemp(prefix="mvdb_split_")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir)
    driver = Driver(["/mnt/e/mvdb/build/minivectordb", "--data-dir", data_dir])
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

        shape_times = {"unfiltered": [], "filtered": []}
        for sql, _, category in probes[:120]:
            start = time.perf_counter()
            driver.send(sql)
            driver.flush()
            driver.read_block()
            elapsed = time.perf_counter() - start
            shape_times["filtered" if category is not None else "unfiltered"].append(elapsed)

        for shape, times in shape_times.items():
            times.sort()
            median = times[len(times) // 2]
            print("%-11s n=%3d  median %6.1f us  p90 %6.1f us" % (
                shape, len(times), median * 1e6, times[int(len(times) * 0.9)] * 1e6))
    finally:
        driver.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
