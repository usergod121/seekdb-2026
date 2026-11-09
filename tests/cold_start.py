#!/usr/bin/env python3
"""Times the first query after a restart (cold row cache) against the ones that follow."""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scale_check import Driver, gen  # noqa: E402

ROWS = 10000
DIMS = 128


def main():
    created, batches, vectors, probes = gen(ROWS, 20, DIMS)
    tmp = tempfile.mkdtemp(prefix="mvdb_cold_")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir)

    driver = Driver(["/mnt/e/mvdb/build/minivectordb", "--data-dir", data_dir])
    driver.send(created)
    driver.flush()
    driver.read_ack()
    start = time.perf_counter()
    for batch in batches:
        driver.send("BEGIN;")
        driver.send("INSERT INTO docs VALUES " + ",".join(batch) + ";")
        driver.send("COMMIT;")
        driver.flush()
        for _ in range(3):
            driver.read_ack()
    load = time.perf_counter() - start
    driver.close()

    # restart, then time the very first query (which has to build the row cache) and the next ones
    driver = Driver(["/mnt/e/mvdb/build/minivectordb", "--data-dir", data_dir])
    try:
        start = time.perf_counter()
        driver.send(probes[0][0])
        driver.flush()
        driver.read_block()
        first = time.perf_counter() - start

        warm = []
        for sql, _, _ in probes[1:12]:
            start = time.perf_counter()
            driver.send(sql)
            driver.flush()
            driver.read_block()
            warm.append(time.perf_counter() - start)
        warm.sort()
        print("load (100 transactions): %8.1f ms" % (load * 1000))
        print("first query after restart: %8.1f ms   <-- row cache build" % (first * 1000))
        print("warm query median:         %8.1f us" % (warm[len(warm) // 2] * 1e6))
        print("build share of the first query: %.1f ms per 10k rows" % (
            (first - warm[len(warm) // 2]) * 1000))
    finally:
        driver.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
