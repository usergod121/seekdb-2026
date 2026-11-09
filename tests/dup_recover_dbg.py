#!/usr/bin/env python3
"""Small reproduction of the duplicate-row recovery with the engine's own recovery diagnostics."""
import os
import shutil
import signal
import subprocess
import tempfile

BIN = "/mnt/e/mvdb/build/minivectordb"
env = dict(os.environ, MVDB_DEBUG_RECOVER="1")


def open_engine(data_dir):
    log = open(os.path.join(os.path.dirname(data_dir), "engine.log"), "ab")
    proc = subprocess.Popen([BIN, "--data-dir", data_dir], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=log, bufsize=0, env=env)
    # wait for READY
    buf = b""
    while b"\n" not in buf:
        chunk = os.read(proc.stdout.fileno(), 4096)
        if not chunk:
            break
        buf += chunk
    return proc, buf


def send(proc, sql):
    os.write(proc.stdin.fileno(), (sql + "\n").encode("utf-8"))


def drain(proc, count):
    """Reads `count` response records (each ends with OK/END/ERROR line)."""
    seen = 0
    buf = b""
    while seen < count:
        chunk = os.read(proc.stdout.fileno(), 65536)
        if not chunk:
            break
        buf += chunk
        seen = buf.count(b"\nOK ") + buf.count(b"\nOK\n")
        if buf.endswith(b"\n"):
            seen = max(seen, sum(1 for line in buf.split(b"\n")
                                 if line.startswith(b"OK") or line == b"END"
                                 or line.startswith(b"ERROR")))
    return buf.decode("utf-8", "replace")


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_dup2_")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir)
    proc, _ = open_engine(data_dir)
    send(proc, "CREATE TABLE docs (id BIGINT PRIMARY KEY, emb VECTOR(16));")
    drain(proc, 1)
    batches = 6
    for batch in range(batches):
        send(proc, "BEGIN;")
        rows = ",".join("(%d,'[%s]')" % (batch * 100 + i + 1, ",".join("%.4f" % ((i * j) % 7 / 7.0)
                                                                       for j in range(16)))
                        for i in range(100))
        send(proc, "INSERT INTO docs VALUES " + rows + ";")
        send(proc, "COMMIT;")
        drain(proc, 2)
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    # Keep the crash-time image untouched: recovery would otherwise checkpoint over it.
    crash_copy = os.path.join(tmp, "crashcopy")
    shutil.copytree(data_dir, crash_copy)
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        print("file %-16s %d bytes" % (name, os.path.getsize(path)))

    proc2, _ = open_engine(data_dir)
    send(proc2, "SELECT * FROM docs;")
    out = drain(proc2, 1)
    rows = [line for line in out.splitlines() if line and not line.startswith(("id", "END", "READY"))
            and not line.startswith("OK")]
    print("recovered rows: %d" % len(rows))
    log_path = os.path.join(os.path.dirname(data_dir), "engine.log")
    print("---- engine log (recovery diagnostics)")
    for line in open(log_path, encoding="utf-8", errors="replace"):
        if line.startswith("[open]") or line.startswith("[replayed]"):
            print(line.rstrip())
    print("kept data dir:", data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
