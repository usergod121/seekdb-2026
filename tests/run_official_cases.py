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
        self.seq = 0
        self.mode = "exact"
        line = self._line()
        try:
            hello = json.loads(line)
        except Exception:
            raise RuntimeError("handshake is not JSON: %r" % line[:120])
        if hello.get("type") != "ready":
            raise RuntimeError("expected a ready handshake, got %r" % line[:120])

    def _line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 16)
            if not chunk:
                raise RuntimeError("engine closed its output")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def run(self, sql):
        self.seq += 1
        request = {"id": self.seq, "op": "execute", "sql": sql, "search_mode": self.mode}
        os.write(self.proc.stdin.fileno(), (json.dumps(request) + "\n").encode("utf-8"))
        reply = json.loads(self._line())
        out = {"kind": None, "columns": None, "rows": [], "affected": None, "code": None, "text": None}
        if reply.get("ok") is True:
            if "columns" in reply:
                out["kind"] = "rows"
                out["columns"] = list(reply.get("columns") or [])
                out["rows"] = list(reply.get("rows") or [])
            else:
                out["kind"] = "ok"
                out["affected"] = int(reply.get("affected_rows") or 0)
        else:
            err = reply.get("error") or {}
            out["kind"] = "error"
            out["code"] = err.get("code") or ""
            out["text"] = err.get("message") or ""
        return out

    def close(self):
        try:
            self.seq += 1
            os.write(self.proc.stdin.fileno(),
                     (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode("utf-8"))
            self._line()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def cell_equal(expected, actual):
    if expected is None:
        return actual is None
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual == expected
    if isinstance(expected, (int, float)):
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return False
        diff = abs(float(actual) - float(expected))
        return diff <= TOL or diff <= TOL * abs(float(expected))
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(expected) == len(actual)
                and all(cell_equal(e, a) for e, a in zip(expected, actual)))
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
            fails.append("affected_rows %r != %r" % (got["affected"], expect["affected_rows"]))
    if "columns" in expect:
        if got["kind"] != "rows":
            return fails + ["expected a query result, got %s" % got["kind"]]
        if got["columns"] != expect["columns"]:
            fails.append("columns %r != %r" % (got["columns"], expect["columns"]))
        if "rows" not in expect:
            return fails          # the expectation only pins the column names, like the judge does
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
                    mode = line.split()[1] if len(line.split()) > 1 else ""
                    modes.append(mode)
                    if mode in ("exact", "ann"):
                        session.mode = mode
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
