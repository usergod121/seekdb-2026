#!/usr/bin/env python3
"""Replays the official public package (20 cases, driver directives included) against the engine.

The package README defines the contract this follows: one case per fresh data directory, one statement
per line, `--restart` and `--mode ann` are driver directives that do not consume an assertion slot, an
`error` assertion means the statement must fail with a non-empty error code, and floats compare with a
1e-5 absolute or relative tolerance.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

CASES = os.environ.get("MVDB_CASES", "/mnt/e/pw/round2/mysqltest2")
BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
TOL = 1e-5


class Session:
    def __init__(self, data_dir):
        self.proc = subprocess.Popen([BIN, "--data-dir", data_dir], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""
        line = self._line()
        if line != "READY":
            raise RuntimeError("expected READY, got %r" % line)

    def _line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 16)
            if not chunk:
                raise RuntimeError("engine closed its output")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def run(self, sql):
        os.write(self.proc.stdin.fileno(), (sql + "\n").encode("utf-8"))
        out = {"kind": None, "columns": None, "rows": [], "affected": None, "code": None, "text": None}
        while True:
            line = self._line()
            if line == "READY":
                continue
            if line.startswith("OK"):
                parts = line.split(" ", 1)
                out["kind"] = "ok"
                out["affected"] = int(parts[1]) if len(parts) > 1 and parts[1].strip() else 0
                return out
            if line.startswith("ERROR"):
                parts = line.split(" ", 2)
                out["kind"] = "error"
                out["code"] = parts[1] if len(parts) > 1 else ""
                out["text"] = parts[2] if len(parts) > 2 else ""
                return out
            if line == "END":
                out["kind"] = "rows"
                return out
            if out["columns"] is None:
                out["columns"] = [] if line == "" else line.split("\t")
            else:
                out["rows"].append([] if line == "" else line.split("\t"))

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
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
    return len(expected) == len(actual) and all(cell_equal(e, a) for e, a in zip(expected, actual))


def check(expect, got, warnings, label):
    fails = []
    if "error" in expect:
        if got["kind"] != "error":
            return ["expected an error, got %s" % got["kind"]]
        if not got["code"]:
            fails.append("error code is empty")
        want = str(expect["error"])
        if got["code"] != want:
            warnings.append("%s: error name %r, package names it %r" % (label, got["code"], want))
        return fails
    if got["kind"] == "error":
        return ["engine reported %s %s" % (got["code"], got["text"])]
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
            remaining = list(got["rows"])
            for want in want_rows:
                for index, cand in enumerate(remaining):
                    if row_equal(want, cand):
                        del remaining[index]
                        break
                else:
                    fails.append("missing unordered row %r" % (want,))
            if remaining:
                fails.append("unexpected unordered rows %r" % (remaining,))
        else:
            if len(want_rows) != len(got["rows"]):
                fails.append("row count %d != %d" % (len(got["rows"]), len(want_rows)))
            for index, (want, have) in enumerate(zip(want_rows, got["rows"])):
                if not row_equal(want, have):
                    fails.append("row %d %r != %r" % (index, have, want))
    return fails


def run_case(case, keep_dir=None):
    root = CASES
    test_path = os.path.join(root, case["test"])
    result_path = os.path.join(root, case["result"])
    lines = [ln.strip() for ln in open(test_path, encoding="utf-8-sig").read().splitlines() if ln.strip()]
    expects = json.load(open(result_path, encoding="utf-8-sig"))

    failures = []
    warnings = []
    passed = 0
    restarts = 0
    modes = []

    tmp = keep_dir or tempfile.mkdtemp(prefix="mvdb_case_")
    data_dir = os.path.join(tmp, "data")
    if keep_dir:
        shutil.rmtree(data_dir, ignore_errors=True)
    os.makedirs(data_dir, exist_ok=True)
    session = Session(data_dir)
    sql_index = 0
    try:
        for line in lines:
            if line.startswith("--"):
                directive = line.split()[0]
                if directive == "--restart":
                    session.close()
                    session = Session(data_dir)
                    restarts += 1
                elif directive == "--mode":
                    modes.append(line.split()[1] if len(line.split()) > 1 else "")
                else:
                    warnings.append("unknown directive %r" % line)
                continue
            if sql_index >= len(expects):
                failures.append("more statements than assertions at %r" % line)
                break
            expect = expects[sql_index]
            got = session.run(line)
            problems = check(expect, got, warnings, "%s[%d]" % (case["id"], sql_index))
            if problems:
                failures.append("%s[%d] %s" % (case["id"], sql_index, line))
                failures.extend("    " + p for p in problems)
            else:
                passed += 1
            sql_index += 1
        if sql_index != len(expects):
            failures.append("%s: %d statements but %d assertions" % (case["id"], sql_index, len(expects)))
    finally:
        session.close()
        if not keep_dir:
            shutil.rmtree(tmp, ignore_errors=True)
    return failures, warnings, passed, restarts, modes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    manifest = json.load(open(os.path.join(CASES, "manifest.json"), encoding="utf-8-sig"))
    cases = [c for c in manifest["cases"] if args.case is None or c["id"] == args.case]

    total_failures = []
    total_passed = 0
    total_statements = 0
    for case in cases:
        failures, warnings, passed, restarts, modes = run_case(case)
        total_passed += passed
        total_statements += case["statements"]
        total_failures.extend(failures)
        flag = "FAIL" if failures else "PASS"
        extra = []
        if restarts:
            extra.append("%d restart(s)" % restarts)
        if modes:
            extra.append("modes=%s" % ",".join(modes))
        print("%-32s %s  %d/%d%s" % (case["id"], flag, passed, case["statements"],
                                     ("  " + " ".join(extra)) if extra else ""))
        for f in failures:
            print("  " + f)
        for w in warnings:
            print("  (note) " + w)

    print()
    bad = len([f for f in total_failures if not f.startswith("    ")])
    print("%d/%d statement(s) passed across %d case(s), %d failure(s)" % (
        total_passed, total_statements, len(cases), bad))
    return 1 if total_failures else 0


if __name__ == "__main__":
    sys.exit(main())
