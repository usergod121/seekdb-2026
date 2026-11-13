#!/usr/bin/env python3
"""Is the commit flush paying for file growth or for data?

Writes 100 records of about 97 KB, each followed by fdatasync, in three ways: to a file that grows each
time, to a file preallocated up front, and to a preallocated file with the same data. If preallocation is
much faster, the metadata update of a growing file is the expensive part and the write-ahead log should
reserve space ahead of time.
"""
import os
import tempfile
import time

RECORD = b"x" * (97 * 1024)
COUNT = 100


def timed(path, preallocate, sparse_write=False):
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    if preallocate:
        os.ftruncate(fd, preallocate)
        os.lseek(fd, 0, os.SEEK_SET)
    started = time.perf_counter()
    for _ in range(COUNT):
        os.write(fd, RECORD)
        os.fdatasync(fd)
    elapsed = (time.perf_counter() - started) * 1000.0
    os.close(fd)
    return elapsed


def main():
    data = tempfile.mkdtemp(prefix="mvdb_fsync_")
    growing = timed(os.path.join(data, "grow.wal"), 0)
    prealloc = timed(os.path.join(data, "pre.wal"), (COUNT + 8) * len(RECORD))
    print("growing file:      %8.1f ms total  %6.2f ms per commit" % (growing, growing / COUNT))
    print("preallocated file: %8.1f ms total  %6.2f ms per commit" % (prealloc, prealloc / COUNT))
    if prealloc > 0:
        print("ratio: %.2fx" % (growing / prealloc))


if __name__ == "__main__":
    main()
