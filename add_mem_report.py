p = "src/db.cpp"
s = open(p, encoding="utf-8").read()
old = """      if (FILE* io = std::fopen("/proc/self/io", "re")) {"""
new = """      if (FILE* status = std::fopen("/proc/self/status", "re")) {
        char line[256];
        while (std::fgets(line, sizeof(line), status) != nullptr) {
          if (std::strncmp(line, "VmHWM:", 6) == 0 || std::strncmp(line, "VmRSS:", 6) == 0) {
            std::fprintf(stderr, "[profile] mem %s", line);
          }
        }
        std::fclose(status);
      }
      if (FILE* io = std::fopen("/proc/self/io", "re")) {"""
assert old in s
s = s.replace(old, new, 1)
open(p, "w", encoding="utf-8", newline="\n").write(s)
print("memory report added")
