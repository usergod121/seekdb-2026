#!/usr/bin/env python3
"""Protocol fuzz: malformed requests must never kill the process or corrupt stdout.

The judge reports a case as "database exited before response" when the process is gone, so anything the
transport can send must leave the engine running and answering. This probe throws random bytes, broken
JSON, wrong field types, missing fields, huge ids, unexpected fields and duplicated keys at the engine
and then checks that it still answers a well-formed request and that every line it wrote on stdout was
valid JSON.
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

BIN = os.environ.get("MVDB_BIN", "/mnt/e/mvdb/build/minivectordb")
PROBLEMS = []
CHECKS = [0]


class Engine:
    def __init__(self, data):
        self.proc = subprocess.Popen([BIN, "--data-dir", data], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""
        self.lines = []
        if self.line() is None:
            raise RuntimeError("no handshake")

    def line(self):
        while b"\n" not in self.buf:
            chunk = os.read(self.proc.stdout.fileno(), 1 << 20)
            if not chunk:
                return None
            self.buf += chunk
        out, _, self.buf = self.buf.partition(b"\n")
        text = out.decode("utf-8", "replace").rstrip("\r")
        self.lines.append(text)
        return text

    def send_only(self, payload):
        if self.proc.poll() is None:
            try:
                os.write(self.proc.stdin.fileno(), payload + b"\n")
            except BrokenPipeError:
                pass

    def send_raw(self, payload):
        if self.proc.poll() is not None:
            return None
        try:
            os.write(self.proc.stdin.fileno(), payload + b"\n")
        except BrokenPipeError:
            return None
        return self.line()

    def close(self):
        if self.proc.poll() is None:
            try:
                os.write(self.proc.stdin.fileno(),
                         (json.dumps({"id": 0, "op": "shutdown"}) + "\n").encode())
                self.line()
                self.proc.wait(timeout=60)
            except Exception:
                self.proc.kill()


def check(label, condition, detail=""):
    CHECKS[0] += 1
    if not condition:
        PROBLEMS.append(label + ("  " + detail if detail else ""))
        print("FAIL  " + label + ("  " + detail if detail else ""))


def main():
    tmp = tempfile.mkdtemp(prefix="mvdb_protofuzz_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    rng = random.Random(20261112)
    try:
        engine = Engine(data)
        engine.send_raw(json.dumps({"id": 1, "op": "execute",
                                    "sql": "CREATE TABLE t (id BIGINT PRIMARY KEY, s VARCHAR(8))"})
                        .encode())

        payloads = [
            b"",                                                     # blank line
            b"   ",
            b"not json at all",
            b"{",
            b"[]",
            b"null",
            b"3",
            b'"a string"',
            b'{"op":"execute"}',                                     # no id, no sql
            b'{"id":1}',                                             # no op
            b'{"id":1,"op":"execute"}',                              # no sql
            b'{"id":1,"op":"execute","sql":123}',                    # sql not a string
            b'{"id":null,"op":"execute","sql":"SELECT id FROM t"}',
            b'{"id":"a-string","op":"execute","sql":"SELECT id FROM t"}',
            b'{"id":1.5,"op":"execute","sql":"SELECT id FROM t"}',
            b'{"id":true,"op":"execute","sql":"SELECT id FROM t"}',
            b'{"id":99999999999999999999999999,"op":"execute","sql":"SELECT id FROM t"}',
            b'{"id":1,"op":"execute","sql":"","search_mode":"exact"}',
            b'{"id":1,"op":"execute","sql":"   "}',
            b'{"id":1,"op":"execute","sql":"SELECT id FROM t","search_mode":"bogus"}',
            b'{"id":1,"op":"execute","sql":"SELECT id FROM t","search_mode":7}',
            b'{"id":1,"op":"nonsense","sql":"SELECT id FROM t"}',
            b'{"id":1,"op":"","sql":"SELECT id FROM t"}',
            b'{"id":1,"op":null,"sql":"SELECT id FROM t"}',
            b'{"id":1,"op":"execute","sql":"SELECT id FROM t","extra":{"deep":[1,2,3]}}',
            b'{"id":1,"id":2,"op":"execute","sql":"SELECT id FROM t"}',   # duplicate key
            b'{"id":1,"op":"execute","sql":"SELECT id FROM t"} trailing',
            b'{"id":1,"op":"execute","sql":"SELECT id FROM t"}\x00',
            b'{"id":1,"op":"execute","sql":"SELECT id FROM t","nan":NaN}',
            b'{"id":1,"op":"execute","sql":"SELECT id FROM t","inf":Infinity}',
            b'{"id":1,"op":"execute","sql":"\xff\xfe"}',
            b'{"id":1,"op":"execute","sql":"SELECT id FROM t"}' + b" " * 4096,
        ]
        for _ in range(200):
            length = rng.randint(1, 400)
            payloads.append(bytes(rng.randrange(1, 256) for _ in range(length)))
        for _ in range(50):
            payloads.append(json.dumps({
                "id": rng.randint(-(2 ** 40), 2 ** 40),
                "op": rng.choice(["execute", "shutdown?", "EXECUTE"]),
                "sql": "".join(rng.choice("SELECTidFROMt WHERE0<>'\"{}[],:")
                               for _ in range(rng.randint(0, 60))),
                "search_mode": rng.choice(["exact", "ann", "", "EXACT"]),
            }).encode())

        for index, payload in enumerate(payloads):
            if payload.strip() == b"":
                # a blank line is not a request: the engine must not answer it, so do not wait
                engine.send_only(payload)
                continue
            reply = engine.send_raw(payload)
            if reply is None:
                check("request %d survived: %r" % (index, payload[:60]), False, "process gone")
                return 1
            if payload.strip() == b"":
                continue          # a blank line is not a request and gets no reply
            try:
                parsed = json.loads(reply)
            except Exception:
                check("request %d produced JSON: %r -> %r" % (index, payload[:40], reply[:80]),
                      False, "unparsable reply")
                continue
            check("request %d reply has ok" % index, "ok" in parsed, reply[:120])
            if "id" in parsed:
                check("request %d echo type" % index,
                      not isinstance(parsed["id"], (list, dict)), reply[:120])

        # after all of that the engine still works; replies to fuzz payloads may still be queued, so
        # the answer is matched by its id instead of by arrival order
        def request(sql, ident):
            if engine.proc.poll() is not None:
                return None
            os.write(engine.proc.stdin.fileno(),
                     (json.dumps({"id": ident, "op": "execute", "sql": sql}) + "\n").encode())
            for _ in range(1000):
                raw = engine.line()
                if raw is None:
                    return None
                try:
                    parsed = json.loads(raw)
                except Exception:
                    continue
                if parsed.get("id") == ident:
                    return parsed
            return None

        reply = request("INSERT INTO t VALUES (1,'a')", 9001)
        check("engine still writes after the fuzz",
              reply is not None and reply.get("ok") is True, str(reply)[:120])
        reply = request("SELECT id FROM t", 9002)
        check("engine still reads after the fuzz",
              reply is not None and reply.get("ok") is True and reply.get("rows") == [[1]],
              str(reply)[:120])
        engine.close()
        check("clean shutdown after the fuzz", engine.proc.returncode == 0,
              "exit=%r" % (engine.proc.returncode,))

        # every line on stdout was a JSON object
        bad = [line for line in engine.lines if line and not line.startswith("{")]
        check("stdout carried only JSON objects", not bad, str(bad[:3]))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
    print()
    print("checks: %d" % CHECKS[0])
    if PROBLEMS:
        print("problems: %d" % len(PROBLEMS))
        for item in PROBLEMS[:25]:
            print("  - " + item)
    else:
        print("no problems")
    sys.exit(1 if PROBLEMS else 0)
