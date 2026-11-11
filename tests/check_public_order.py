#!/usr/bin/env python3
"""Checks what the public cases expect when a SELECT has no ORDER BY.

If such a query returns more than one row, its expected order tells us what the judge assumes: the
insertion order of the rows, or the primary-key order (what a clustered index would produce). The
distinction matters because an engine that scans a heap returns insertion order while a case written
against MySQL may assume primary-key order.
"""
import json
import os
import re
import sys

CASES = os.environ.get("MVDB_CASES", "/mnt/e/pw/round2/pkg2/mysqltest")


def main():
    manifest = json.load(open(os.path.join(CASES, "manifest.json"), encoding="utf-8-sig"))
    interesting = 0
    for case in manifest["cases"]:
        test = open(os.path.join(CASES, case["test"]), encoding="utf-8-sig").read().splitlines()
        result = json.load(open(os.path.join(CASES, case["result"]), encoding="utf-8-sig"))
        statements = [line.strip() for line in test if line.strip() and not line.strip().startswith("--")]
        # track the insert order of primary keys, so the two orders can be compared
        insert_order = []
        for sql, expect in zip(statements, result):
            lowered = sql.lower()
            if lowered.startswith("insert"):
                for match in re.finditer(r"\(\s*(-?\d+)\s*,", sql):
                    insert_order.append(int(match.group(1)))
                continue
            rows = expect.get("rows")
            if rows is None or len(rows) < 2:
                continue
            ids = [row[0] for row in rows if row and isinstance(row[0], int)]
            if len(ids) < 2:
                continue
            ordered = "order by" in lowered
            unordered_flag = expect.get("unordered", False)
            print("%-32s %s" % (case["id"], sql[:70]))
            print("      expected ids: %r  ordered_query=%s unordered_flag=%s"
                  % (ids, ordered, unordered_flag))
            print("      insert order: %r  pk sorted: %r"
                  % (insert_order[:len(ids)], sorted(insert_order)[:len(ids)]))
            interesting += 1
    print()
    print("multi-row expectations without an explicit ORDER BY:",
          interesting if interesting else "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
