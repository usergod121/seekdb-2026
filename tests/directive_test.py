#!/usr/bin/env python3
"""Sends driver directives on the SQL channel and waits for an answer, the way a driver might."""
import os
import shutil
import subprocess
import sys
import tempfile

BIN = "/mnt/e/mvdb/build/minivectordb"


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_dir_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    state = {"buf": b""}

    def line(timeout_note=""):
        while b"\n" not in state["buf"]:
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                return None
            state["buf"] += chunk
        out, _, state["buf"] = state["buf"].partition(b"\n")
        return out.decode("utf-8", "replace").rstrip("\r")

    def send(text):
        os.write(proc.stdin.fileno(), text.encode("utf-8"))

    problems = []
    ready = line()
    print("startup: %r" % ready)
    if ready != "READY":
        problems.append("no READY")

    for statement, expected in [("CREATE TABLE t (id BIGINT PRIMARY KEY);", "OK 0"),
                                ("INSERT INTO t VALUES (1);", "OK 1")]:
        send(statement + "\n")
        got = line()
        print("%-45s -> %r" % (statement, got))
        if got != expected:
            problems.append("%s answered %r" % (statement, got))

    for directive in ["--mode ann\n", "--restart\n", "--mode exact\n"]:
        send(directive)
        got = line()
        print("directive %-34r -> %r" % (directive, got))
        if got is None or not got.startswith("OK"):
            problems.append("directive %r was not acknowledged (%r)" % (directive.strip(), got))

    send("SELECT id FROM t;\n")
    header = line()
    row = line()
    end = line()
    print("query after directives -> %r %r %r" % (header, row, end))
    if (header, row, end) != ("id", "1", "END"):
        problems.append("query after directives returned %r %r %r" % (header, row, end))

    send("quit\n")
    tail = line()
    try:
        code = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = "timeout"
    print("shutdown on 'quit' -> next line %r, exit %r" % (tail, code))
    if tail != "BYE" or code != 0:
        problems.append("quit did not shut down cleanly (%r, %r)" % (tail, code))

    shutil.rmtree(tmp, ignore_errors=True)
    for problem in problems:
        print("FAIL " + problem)
    print()
    print("%d directive problem(s)" % len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
