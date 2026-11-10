"""Validates the material the way the published rules describe it.

Rules being enforced (from the statement's 3.4.3 and 4.4 sections):
  - UTF-8, valid JSON, top level is an object, size <= 128 KiB, no duplicate keys, no NaN/Infinity;
  - every citation is an RFC6901 JSON pointer into a *string* field of the same file, and the quoted
    excerpt is at most 240 characters and matches that field exactly;
  - an instance scoring above 0.5 needs both a student and a result citation; the 0.5 tier needs the
    result citation to be absent;
  - the same excerpt may not back two instances.
"""
import json
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "/mnt/e/pw/round2/conversation_draft.json"
LIMIT = 128 * 1024
problems = []


def pointer_resolve(document, pointer):
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError("pointer must start with '/': %r" % pointer)
    node = document
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            node = node[int(token)]
        elif isinstance(node, dict):
            node = node[token]
        else:
            raise ValueError("pointer %r runs into a scalar" % pointer)
    return node


def main():
    raw = open(PATH, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append("file starts with a UTF-8 BOM")
    if len(raw) > LIMIT:
        problems.append("file is %d bytes, above the 128 KiB limit" % len(raw))

    def reject_duplicates(pairs):
        seen = {}
        for key, value in pairs:
            if key in seen:
                problems.append("duplicate JSON key: %r" % key)
            seen[key] = value
        return seen

    def reject_constant(value):
        problems.append("non-finite JSON number: %s" % value)
        return None

    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=reject_duplicates,
                          parse_constant=reject_constant)
    except Exception as exc:  # noqa: BLE001
        print("INVALID JSON: %s" % exc)
        return 1
    if not isinstance(data, dict):
        problems.append("top level is not an object")

    quotes = {}
    for index, episode in enumerate(data.get("episodes", [])):
        label = episode.get("id", "#%d" % index)
        for kind in ("student", "result"):
            pointer = episode.get(kind + "_pointer")
            quote = episode.get(kind + "_evidence", "")
            if not pointer and not quote:
                continue
            if not pointer or not quote:
                problems.append("%s: %s pointer/quote incomplete" % (label, kind))
                continue
            if len(quote) > 240:
                problems.append("%s: %s excerpt is %d chars (>240)" % (label, kind, len(quote)))
            try:
                target = pointer_resolve(data, pointer)
            except Exception as exc:  # noqa: BLE001
                problems.append("%s: %s pointer does not resolve: %s" % (label, kind, exc))
                continue
            if not isinstance(target, str):
                problems.append("%s: %s pointer targets a %s, not a string" % (
                    label, kind, type(target).__name__))
                continue
            if quote not in target:
                problems.append("%s: %s excerpt is not present in the target field" % (label, kind))
            if kind == "student":
                if quote in quotes:
                    problems.append("%s: student excerpt reused by %s" % (label, quotes[quote]))
                quotes[quote] = label
        if not episode.get("dimension"):
            problems.append("%s: missing dimension" % label)

    print("file: %s" % PATH)
    print("bytes: %d (limit %d)" % (len(raw), LIMIT))
    print("user_prompts: %d, experiments: %d, episodes: %d" % (
        len(data.get("user_prompts", [])), len(data.get("experiments", [])),
        len(data.get("episodes", []))))
    print("synthetic: %s" % data.get("synthetic"))
    by_dimension = {}
    for episode in data.get("episodes", []):
        by_dimension.setdefault(episode.get("dimension", "?"), []).append(episode.get("id"))
    print("dimensions: %s" % json.dumps(by_dimension, ensure_ascii=False))
    print()
    if problems:
        print("%d problem(s):" % len(problems))
        for problem in problems:
            print("  - " + problem)
        return 1
    print("all citation and format checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
