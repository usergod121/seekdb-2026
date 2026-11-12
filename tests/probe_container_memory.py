#!/usr/bin/env python3
"""Runs the engine inside the judge's container flags and watches its memory.

The judge kills the container when it goes over 2 GiB, and a killed process is reported as
"database exited before response", so this probe drives the image with the exact flags and reports the
container's peak memory for the shapes that allocate the most: a table of long strings, a large vector
table and a large reply.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

IMAGE = os.environ.get("MVDB_IMAGE", "mvdb:v14")
CONTAINER = "mvdb-memtest"
FAILURES = []


class Container:
    def __init__(self, data_dir):
        subprocess.run(["docker", "rm", "--force", "--volumes", CONTAINER],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.proc = subprocess.Popen([
            "docker", "run", "--name", CONTAINER, "--interactive", "--rm",
            "--network=none", "--cpus=2", "--memory=2g", "--memory-swap=2g",
            "--pids-limit=128", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--read-only", "--mount", "type=bind,src=%s,dst=/data" % data_dir,
            IMAGE,
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.buf = b""
        self.seq = 0
        if self.line() is None:
            raise RuntimeError("container produced no handshake")

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 20)
            if not chunk:
                return None
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace").rstrip("\r")

    def req(self, sql):
        if self.proc.poll() is not None:
            return None
        self.seq += 1
        payload = json.dumps({"id": self.seq, "op": "execute", "sql": sql, "search_mode": "exact"})
        os.write(self.proc.stdin.fileno(), (payload + "\n").encode())
        raw = self.line()
        if raw is None:
            return None
        return json.loads(raw)

    def peak_memory(self):
        out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}",
                              CONTAINER], stdout=subprocess.PIPE, text=True).stdout.strip()
        return out

    def close(self):
        try:
            self.seq += 1
            os.write(self.proc.stdin.fileno(),
                     (json.dumps({"id": self.seq, "op": "shutdown"}) + "\n").encode())
            self.line()
            self.proc.wait(timeout=180)
        except Exception:
            subprocess.run(["docker", "rm", "--force", "--volumes", CONTAINER],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def check(label, condition, detail=""):
    if not condition:
        FAILURES.append("%s  %s" % (label, detail))
        print("FAIL  %s  %s" % (label, detail))
    else:
        print("ok    " + label)


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_mem_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    container = Container(data)
    try:
        check("handshake inside the judge container", True)

        # 1. 20000 rows of 1000-character text: 20 MB of payload, decoded copies included
        check("create the long-string table",
              container.req("CREATE TABLE ls (id BIGINT PRIMARY KEY, body VARCHAR(2048))")
              .get("ok") is True)
        for start in range(1, 20001, 500):
            values = ",".join("(%d,'%s')" % (i, "y" * 1000) for i in range(start, start + 500))
            reply = container.req("INSERT INTO ls VALUES " + values)
            if reply is None:
                check("insert batch at %d" % start, False, "process gone")
                break
        reply = container.req("SELECT id FROM ls LIMIT 1")
        check("query the long-string table", reply is not None and reply.get("ok") is True)
        print("      memory after the long strings: " + container.peak_memory())

        # 2. a reply carrying every row (about 20 MB of JSON, past the driver's 16 MiB limit but it
        #    must not kill the process)
        reply = container.req("SELECT id, body FROM ls")
        check("huge reply answered", reply is not None and reply.get("ok") is True,
              "process gone" if reply is None else "rows=%d" % len(reply.get("rows") or []))
        print("      memory after the huge reply: " + container.peak_memory())

        # 3. a vector table, TopK over it, then a restart
        check("create the vector table",
              container.req("CREATE TABLE vec (id BIGINT PRIMARY KEY, emb VECTOR(128))")
              .get("ok") is True)
        for start in range(1, 20001, 500):
            values = ",".join("(%d,'[%s]')" % (
                i, ",".join("%.3f" % (((i * (d + 3)) % 89) / 89.0) for d in range(128)))
                for i in range(start, start + 500))
            reply = container.req("INSERT INTO vec VALUES " + values)
            if reply is None:
                check("vector insert batch at %d" % start, False, "process gone")
                break
        query = "[" + ",".join("0.5" for _ in range(128)) + "]"
        reply = container.req("SELECT id FROM vec ORDER BY L2_DISTANCE(emb,'%s') LIMIT 10" % query)
        check("TopK over 20000 vectors", reply is not None and reply.get("ok") is True)
        print("      memory after the vector load: " + container.peak_memory())
        code = container.close()
        return_code = container.proc.returncode
        check("clean shutdown", return_code == 0, "exit=%r" % (return_code,))
    finally:
        try:
            subprocess.run(["docker", "rm", "--force", "--volumes", CONTAINER],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print("failures: %d" % len(FAILURES))
        for item in FAILURES:
            print("  - " + item)
        return 1
    print("no failures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
