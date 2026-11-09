#!/usr/bin/env python3
"""Dumps what the WAL actually contains for the crash reproduction."""
import os
import shutil
import struct

found = []


def parse(path):
    data = open(path, "rb").read()
    pos = 0
    statements = []
    pending = []
    groups = []
    while pos < len(data):
        tag = data[pos:pos + 1]
        if tag == b"C":
            groups.append(list(pending))
            pending.clear()
            pos += 1
            continue
        if tag != b"S" or pos + 5 > len(data):
            break
        length = struct.unpack_from("<I", data, pos + 1)[0]
        begin = pos + 5
        if begin + length > len(data):
            print("  truncated record at %d" % pos)
            break
        pending.append(data[begin:begin + length].decode("utf-8", "replace"))
        pos = begin + length
    return groups, pending


def main():
    for root, _, files in os.walk("/tmp"):
        for name in files:
            if name == "data.wal" and os.path.getsize(os.path.join(root, name)) > 0:
                found.append(os.path.join(root, name))
    if not found:
        print("no WAL found (run tests/dup_recover_dbg.py first)")
        return 0
    for path in found:
        print("WAL %s (%d bytes)" % (path, os.path.getsize(path)))
        groups, pending = parse(path)
        print("  committed groups: %d, uncommitted records: %d" % (len(groups), len(pending)))
        for index, group in enumerate(groups):
            for sql in group:
                head = sql.strip().replace("\n", " ")[:48]
                rows = sql.count("),(") + 1 if "VALUES" in sql else 0
                print("   group %d: %-50s ~%d row(s)" % (index, head, rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
