"""Checks the conversation-style specimen: format, pointer resolvability, exact excerpt match, length."""
import json
import sys

PATH = sys.argv[1]
LIMIT = 128 * 1024
problems = []


def resolve(document, pointer):
    node = document
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        node = node[int(token)] if isinstance(node, list) else node[token]
    return node


raw = open(PATH, "rb").read()
if raw.startswith(b"\xef\xbb\xbf"):
    problems.append("file starts with a BOM")
if len(raw) > LIMIT:
    problems.append("%d bytes is over the 128 KiB limit" % len(raw))


def dup(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            problems.append("duplicate key %r" % key)
        seen[key] = value
    return seen


def bad_constant(value):
    problems.append("non-finite number %s" % value)
    return None


data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=dup, parse_constant=bad_constant)
if not isinstance(data, dict):
    problems.append("top level is not an object")

seen_quotes = {}
for index, item in enumerate(data.get("证据实例", [])):
    label = item.get("编号", "#%d" % index)
    for field, quote_field in (("学生引用", "学生原话"), ("结果引用", "结果原文")):
        pointer = item.get(field)
        quote = item.get(quote_field, "")
        if not pointer or not quote:
            problems.append("%s: %s/%s incomplete" % (label, field, quote_field))
            continue
        if len(quote) > 240:
            problems.append("%s: %s is %d chars (>240)" % (label, quote_field, len(quote)))
        try:
            target = resolve(data, pointer)
        except Exception as exc:  # noqa: BLE001
            problems.append("%s: %s does not resolve (%s)" % (label, field, exc))
            continue
        if not isinstance(target, str):
            problems.append("%s: %s points at a %s" % (label, field, type(target).__name__))
            continue
        if quote not in target:
            problems.append("%s: %s does not match the target field" % (label, quote_field))
        if quote_field == "学生原话":
            if quote in seen_quotes:
                problems.append("%s: student quote reused from %s" % (label, seen_quotes[quote]))
            seen_quotes[quote] = label

print("file: %s" % PATH)
print("bytes: %d / %d" % (len(raw), LIMIT))
print("synthetic: %s" % data.get("synthetic"))
print("rounds: %d, messages: %d, experiments: %d, evidence: %d" % (
    len(data.get("conversations", [])),
    sum(len(r.get("messages", [])) for r in data.get("conversations", [])),
    len(data.get("experiments", [])), len(data.get("证据实例", []))))
by_source = {}
for round_ in data.get("conversations", []):
    for message in round_.get("messages", []):
        by_source[message.get("source")] = by_source.get(message.get("source"), 0) + 1
print("messages by source: %s" % json.dumps(by_source, ensure_ascii=False))
dims = {}
for item in data.get("证据实例", []):
    dims.setdefault(item.get("维度"), []).append(item.get("档位目标"))
print("dimensions: %s" % json.dumps(dims, ensure_ascii=False))
print()
if problems:
    print("%d problem(s):" % len(problems))
    for problem in problems:
        print("  - " + problem)
    sys.exit(1)
print("all checks passed")
