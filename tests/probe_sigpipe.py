#!/usr/bin/env python3
"""Does the engine survive its stdout disappearing?

The judge reports a case as "database exited before response" when the process is gone; writing to a
pipe whose reader closed raises SIGPIPE, whose default action kills the process. Closing the read end
and then sending work must leave the engine alive (it may answer to a broken pipe, but it must not
die).
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_sigpipe_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        buf = b""

        def line():
            nonlocal buf
            while b"\n" not in buf:
                chunk = os.read(proc.stdout.fileno(), 1 << 16)
                if not chunk:
                    return None
                buf += chunk
            out, _, buf = buf.partition(b"\n")
            return out.decode("utf-8", "replace").rstrip("\r")

        assert line() is not None, "no handshake"

        def req(sql, ident=1):
            os.write(proc.stdin.fileno(),
                     (json.dumps({"id": ident, "op": "execute", "sql": sql}) + "\n").encode())
            return line()

        req("CREATE TABLE t (id BIGINT PRIMARY KEY, s VARCHAR(8))")
        req("INSERT INTO t VALUES (1,'a')")

        # close the reader while the engine is still running, then keep talking to it
        proc.stdout.close()
        for i in range(20):
            try:
                os.write(proc.stdin.fileno(),
                         (json.dumps({"id": 100 + i, "op": "execute",
                                      "sql": "SELECT id FROM t"}) + "\n").encode())
            except BrokenPipeError:
                break

        import time
        time.sleep(1.0)
        alive = proc.poll() is None
        print("engine alive after its stdout was closed:", alive)
        if not alive:
            err = b""
            try:
                err = proc.stderr.read(400) or b""
            except Exception:
                pass
            print("FAIL  the engine died; exit=%r stderr=%r" % (proc.returncode, err))
            return 1
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
            print("clean exit after SIGTERM:", proc.returncode)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("FAIL  the engine ignored SIGTERM")
            return 1
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
