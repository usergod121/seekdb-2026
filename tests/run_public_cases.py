#!/usr/bin/env python3
"""Runs the public mysqltest-style cases against the engine.

Mirrors what tests/README.md describes: every case starts from a fresh empty data directory, each
non-empty line of cases/<name>.test is one statement, and the matching entry of cases/<name>.result is
the expected assertion. A case passes only when every statement succeeds and every assertion holds.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

CASES = os.environ.get("MVDB_CASES", "/mnt/e/pw/round2/mysqltest/cases")
BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
TOL = 1e-5


class Session:
    def __init__(self, data_dir):
        self.proc = subprocess.Popen(
            [BIN, "--data-dir", data_dir],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.buf = b""

    def _line(self):
        while b"\n" not in self.buf:
            chunk = self.proc.stdout.read(1)
            if not chunk:
                raise RuntimeError("engine closed its output")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def ready(self):
        line = self._line()
        if line != "READY":
            raise RuntimeError("expected READY, got %r" % line)
        return line

    def run(self, sql):
        """Sends one statement and parses its response."""
        self.proc.stdin.write((sql + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        out = {"kind": None, "columns": None, "rows": None, "affected": None, "error": None}
        while True:
            line = self._line()
            if line == "READY":
                continue
            if line.startswith("OK "):
                out["kind"] = "ok"
                out["affected"] = int(line.split()[1])
                return out
            if line == "OK":
                out["kind"] = "ok"
                out["affected"] = 0
                return out
            if line.startswith("ERROR"):
                out["kind"] = "error"
                out["error"] = line
                return out
            if line == "END":
                out["kind"] = "rows"
                return out
            if out["columns"] is None:
                out["columns"] = [] if line == "" else line.split("\t")
                out["rows"] = []
            else:
                out["rows"].append([] if line == "" else line.split("\t"))

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def cell_equal(expected, actual):
    if isinstance(expected, bool):
        return str(expected).lower() == actual.lower()
    if isinstance(expected, int):
        try:
            return int(actual) == expected
        except ValueError:
            try:
                return abs(float(actual) - expected) <= TOL * max(1.0, abs(expected))
            except ValueError:
                return False
    if isinstance(expected, float):
        try:
            got = float(actual)
        except ValueError:
            return False
        diff = abs(got - expected)
        return diff <= TOL or diff <= TOL * abs(expected)
    return expected == actual


def row_equal(expected, actual):
    return len(expected) == len(actual) and all(
        cell_equal(e, a) for e, a in zip(expected, actual)
    )


def check(expect, got):
    """Returns a list of failure strings for one statement."""
    fails = []
    if got["kind"] == "error":
        return ["engine reported %s" % got["error"]]
    if "affected_rows" in expect:
        if got["kind"] != "ok":
            return ["expected an affected_rows result, got %s" % got["kind"]]
        if got["affected"] != expect["affected_rows"]:
            fails.append("affected_rows %d != %d" % (got["affected"], expect["affected_rows"]))
    if "columns" in expect:
        if got["kind"] != "rows":
            return fails + ["expected a query result, got %s" % got["kind"]]
        if got["columns"] != expect["columns"]:
            fails.append("columns %r != %r" % (got["columns"], expect["columns"]))
        want_rows = expect["rows"]
        if expect.get("unordered"):
            def key(r):
                return [str(c) for c in r]
            remaining = list(got["rows"])
            for want in want_rows:
                for i, cand in enumerate(remaining):
                    if row_equal(want, cand):
                        del remaining[i]
                        break
                else:
                    fails.append("missing unordered row %r" % (want,))
            if remaining:
                fails.append("unexpected unordered rows %r" % (remaining,))
        else:
            if len(want_rows) != len(got["rows"]):
                fails.append("row count %d != %d" % (len(got["rows"]), len(want_rows)))
            for i, (want, have) in enumerate(zip(want_rows, got["rows"])):
                if not row_equal(want, have):
                    fails.append("row %d %r != %r" % (i, have, want))
    return fails


def run_case(name):
    test_path = os.path.join(CASES, name + ".test")
    result_path = os.path.join(CASES, name + ".result")
    statements = [
        line.strip()
        for line in open(test_path, encoding="utf-8-sig").read().splitlines()
        if line.strip()
    ]
    expects = json.load(open(result_path, encoding="utf-8-sig"))
    if len(statements) != len(expects):
        return ["%s: %d statements but %d assertions" % (name, len(statements), len(expects))], 0

    tmp = tempfile.mkdtemp(prefix="mvdb_case_")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir)
    session = Session(data_dir)
    failures = []
    passed = 0
    try:
        session.ready()
        for index, (sql, expect) in enumerate(zip(statements, expects)):
            got = session.run(sql)
            problems = check(expect, got)
            if problems:
                failures.append("%s[%d] %s" % (name, index, sql))
                failures.extend("    " + p for p in problems)
            else:
                passed += 1
    finally:
        session.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return failures, passed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default=None, help="run one case by name")
    args = ap.parse_args()
    names = [args.case] if args.case else [
        os.path.splitext(f)[0]
        for f in sorted(os.listdir(CASES))
        if f.endswith(".test")
    ]
    names.sort(key=lambda n: [
        "sql_crud", "sql_comparisons", "sql_ordering", "sql_empty", "vector_euclidean"
    ].index(n) if n in [
        "sql_crud", "sql_comparisons", "sql_ordering", "sql_empty", "vector_euclidean"
    ] else 99)

    total_failures = []
    total_passed = 0
    for name in names:
        failures, passed = run_case(name)
        total_passed += passed
        total_failures.extend(failures)
        print("%-20s %s (%d passed)" % (name, "FAIL" if failures else "PASS", passed))
        for f in failures:
            print("  " + f)
    print()
    print("%d statement(s) passed, %d failure(s)" % (total_passed, len([
        f for f in total_failures if not f.startswith("    ")
    ])))
    return 1 if total_failures else 0


if __name__ == "__main__":
    sys.exit(main())
