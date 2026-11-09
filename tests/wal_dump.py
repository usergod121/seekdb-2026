import struct
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mvdb_dup2_rh00hr5b/data/data.wal"
data = open(path, "rb").read()
pos = 0
groups = []
pending = []
while pos < len(data):
    tag = data[pos:pos + 1]
    if tag == b"C":
        groups.append(list(pending))
        pending = []
        pos += 1
        continue
    if tag != b"S" or pos + 5 > len(data):
        break
    length = struct.unpack_from("<I", data, pos + 1)[0]
    begin = pos + 5
    if begin + length > len(data):
        print("truncated record at", pos)
        break
    pending.append(data[begin:begin + length].decode("utf-8", "replace"))
    pos = begin + length
print("file:", path, len(data), "bytes")
print("committed groups:", len(groups), "uncommitted records:", len(pending))
for index, group in enumerate(groups):
    for sql in group:
        head = sql.strip().replace("\n", " ")[:44]
        print("  group %d | %-46s | chars=%d rows~%d" % (
            index, head, len(sql), sql.count("),(") + 1 if "VALUES" in sql else 0))
