#!/usr/bin/env python3
"""Decomposes query cost: fixed round trip, the cost of parsing a 128-dim literal, and the full scan."""
import os
import random
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scale_check import Driver, gen  # noqa: E402

ROWS = 10000
DIMS = 128


def timed(driver, sqls, repeats=40):
    samples = []
    for index in range(repeats):
        sql = sqls[index % len(sqls)]
        start = time.perf_counter()
        driver.send(sql)
        driver.flush()
        driver.read_block()
        samples.append(time.perf_counter() - start)
    samples.sort()
    return samples[len(samples) // 2]


def main():
    created, batches, vectors, probes = gen(ROWS, 60, DIMS)
    rng = random.Random(7)
    big = "[" + ",".join("%.4f" % rng.uniform(-1, 1) for _ in range(DIMS)) + "]"
    tmp = tempfile.mkdtemp(prefix="mvdb_decomp_")
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

        trivial = timed(driver, ["SELECT id FROM docs LIMIT 0;"])
        with_literal = timed(driver, [
            "SELECT id, L2_DISTANCE(emb,'%s') AS d FROM docs ORDER BY d LIMIT 0;" % big])
        with_literal_twice = timed(driver, [
            "SELECT id, L2_DISTANCE(emb,'%s') AS d FROM docs ORDER BY L2_DISTANCE(emb,'%s') LIMIT 0;"
            % (big, big)])
        unfiltered = timed(driver, [
            "SELECT id, L2_DISTANCE(emb,'%s') AS d FROM docs ORDER BY d LIMIT 10;" % big])
        filtered = timed(driver, [
            "SELECT id, L2_DISTANCE(emb,'%s') AS d FROM docs WHERE category = 'c3' ORDER BY d LIMIT 10;"
            % big])
        count_only = timed(driver, ["SELECT id FROM docs;"])
        order_only = timed(driver, ["SELECT id FROM docs ORDER BY id LIMIT 10;"])

        print("%-34s %7.1f us" % ("round trip floor (LIMIT 0)", trivial * 1e6))
        print("%-34s %7.1f us" % ("+ one 128-dim literal (LIMIT 0)", with_literal * 1e6))
        print("%-34s %7.1f us" % ("+ two 128-dim literals (LIMIT 0)", with_literal_twice * 1e6))
        print("%-34s %7.1f us" % ("full scan, unfiltered LIMIT 10", unfiltered * 1e6))
        print("%-34s %7.1f us" % ("full scan, filtered LIMIT 10", filtered * 1e6))
        print("%-34s %7.1f us" % ("materialise all rows", count_only * 1e6))
        print("%-34s %7.1f us" % ("order by id LIMIT 10", order_only * 1e6))
    finally:
        driver.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
