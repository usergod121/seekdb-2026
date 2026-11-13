// Entry point: open the database, announce readiness with the handshake the task package defines, then
// serve JSON-lines requests from stdin and answer one JSON line per request on stdout. Logs go to stderr.
#include <cerrno>
#include <csignal>
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

#include <unistd.h>

#include "db.h"
#include "prof.h"
#include "protocol/json.h"
#include "protocol/writer.h"

namespace {

volatile std::sig_atomic_t g_stop = 0;

void on_signal(int) { g_stop = 1; }

bool parse_args(int argc, char** argv, std::string* data_dir) {
  for (int i = 1; i < argc; ++i) {
    const char* a = argv[i];
    if (std::strcmp(a, "--data-dir") == 0 && i + 1 < argc) {
      *data_dir = argv[++i];
    } else if (std::strncmp(a, "--data-dir=", 11) == 0) {
      *data_dir = a + 11;
    } else if (std::strcmp(a, "--help") == 0 || std::strcmp(a, "-h") == 0) {
      std::fprintf(stderr, "usage: minivectordb --data-dir <dir>\n");
      return false;
    } else {
      std::fprintf(stderr, "unknown argument: %s\n", a);
      return false;
    }
  }
  return true;
}

}  // namespace

// Micro benchmark used to compare engine variants without a client process in the loop.
// MVDB_SELFBENCH=<queries> builds a 10k x 128 table in the given data directory, runs the query mixture
// the graded benchmark uses, and prints per-half statistics to stderr.
static int self_benchmark(mv::Database& db, const int queries, const int rows, const int dims) {
  char buf[512];
  std::snprintf(buf, sizeof(buf),
                "CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), emb VECTOR(%d))", dims);
  mv::Result r = db.execute(buf);
  if (r.err != mv::Err::Ok) { std::fprintf(stderr, "selfbench create failed\n"); return 1; }

  std::uint64_t state = 12345;
  auto next = [&state]() {
    state = state * 6364136223846793005ULL + 1442695040888963407ULL;
    return static_cast<double>((state >> 33) & 0xFFFFFF) / 16777216.0;
  };
  for (int start = 0; start < rows; start += 100) {
    std::string sql = "INSERT INTO docs VALUES ";
    for (int i = start; i < start + 100 && i < rows; ++i) {
      if (i > start) sql += ",";
      sql += "(" + std::to_string(i + 1) + ",'c" + std::to_string(i % 8) + "','[";
      for (int d = 0; d < dims; ++d) {
        if (d > 0) sql += ",";
        std::snprintf(buf, sizeof(buf), "%.4f", next() * 2.0 - 1.0);
        sql += buf;
      }
      sql += "]')";
    }
    mv::Result ins = db.execute(sql);
    if (ins.err != mv::Err::Ok) { std::fprintf(stderr, "selfbench insert failed\n"); return 1; }
  }
  db.execute("SELECT id FROM docs ORDER BY id LIMIT 1");

  std::vector<double> unfiltered, filtered;
  const auto load_started = std::chrono::steady_clock::now();
  for (int q = 0; q < queries; ++q) {
    std::string probe = "[";
    for (int d = 0; d < dims; ++d) {
      if (d > 0) probe += ",";
      std::snprintf(buf, sizeof(buf), "%.4f", next() * 2.0 - 1.0);
      probe += buf;
    }
    probe += "]";
    std::string sql;
    if (q % 2) {
      sql = "SELECT id FROM docs WHERE category='c" + std::to_string(q % 8) +
            "' ORDER BY L2_DISTANCE(emb,'" + probe + "') LIMIT 10";
    } else {
      sql = "SELECT id FROM docs ORDER BY L2_DISTANCE(emb,'" + probe + "') LIMIT 10";
    }
    const auto started = std::chrono::steady_clock::now();
    mv::Result res = db.execute(sql);
    const double us = std::chrono::duration<double, std::micro>(
                          std::chrono::steady_clock::now() - started).count();
    if (res.err != mv::Err::Ok) { std::fprintf(stderr, "selfbench query failed\n"); return 1; }
    (q % 2 ? filtered : unfiltered).push_back(us);
  }
  const double total_ms =
      std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - load_started).count();
  auto report = [](const char* label, std::vector<double> values) {
    if (values.empty()) return;
    std::sort(values.begin(), values.end());
    const double p10 = values[static_cast<size_t>(values.size() / 10)];
    std::fprintf(stderr, "[selfbench] %-10s min %7.2f us  p10 %7.2f us  median %7.2f us  n %zu\n",
                 label, values.front(), p10, values[values.size() / 2], values.size());
  };
  report("unfiltered", unfiltered);
  report("filtered", filtered);
  std::fprintf(stderr, "[selfbench] query-phase total %.2f ms for %d queries\n", total_ms, queries);
  return 0;
}

int main(int argc, char** argv) {
  std::string data_dir = "/data";
  if (!parse_args(argc, argv, &data_dir)) return 2;

  // Deliberately no SA_RESTART: a signal must interrupt the blocking read so shutdown happens promptly.
  // A closed stdout must not kill the process: writing to a pipe whose reader is gone raises
  // SIGPIPE, whose default action is to terminate, and the judge reports that as "database exited
  // before response". The write simply fails and the loop keeps reading until stdin closes.
  std::signal(SIGPIPE, SIG_IGN);
  struct sigaction sa;
  std::memset(&sa, 0, sizeof(sa));
  sa.sa_handler = on_signal;
  sigaction(SIGTERM, &sa, nullptr);
  sigaction(SIGINT, &sa, nullptr);

  mv::Database db;
  mv::Writer w;
  if (db.open(data_dir) != mv::Err::Ok) {
    std::fprintf(stderr, "failed to open data directory: %s\n", data_dir.c_str());
    return 1;
  }
  if (const char* bench = ::getenv("MVDB_SELFBENCH")) {
    const int count = std::atoi(bench);
    if (count > 0) {
      const int rc = self_benchmark(db, count, 10000, 128);
      db.close();
      return rc;
    }
  }

  // Opening the data directory includes recovery, so the handshake goes out before the first request is
  // read, exactly as the protocol requires.
  w.ready();

  std::string pending;
  char buf[65536];
  while (g_stop == 0) {
    const ssize_t n = ::read(STDIN_FILENO, buf, sizeof(buf));
    if (n < 0) {
      if (errno == EINTR) continue;   // signal: the loop condition handles shutdown
      break;
    }
    if (n == 0) break;                // the driver closed stdin
    pending.append(buf, static_cast<size_t>(n));

    bool stop = false;
    size_t newline = 0;
    while ((newline = pending.find('\n')) != std::string::npos) {
      std::string line = pending.substr(0, newline);
      pending.erase(0, newline + 1);
      if (!line.empty() && line.back() == '\r') line.pop_back();
      if (line.empty()) continue;   // a blank line is not a request, and nothing may be sent back

      mv::JsonValue request;
      std::string parse_error;
      const uint64_t prof_json_start = mv::Prof::on() ? mv::Prof::now() : 0;
      if (!mv::json_parse(line, &request, &parse_error)) {
        w.protocol_error("", "invalid_json", parse_error);
        continue;
      }
      if (mv::Prof::on()) mv::Prof::add(mv::kProfJson, mv::Prof::now() - prof_json_start);
      // Echo the id exactly as it arrived, including its JSON type.
      const mv::JsonValue* id = mv::json_member(request, "id");
      const std::string id_json = (id != nullptr && !id->raw.empty()) ? id->raw : std::string("null");

      const mv::JsonValue* op = mv::json_member(request, "op");
      const std::string op_name = (op != nullptr && op->kind == mv::JsonValue::Kind::Str) ? op->s : "";
      if (op_name == "shutdown") {
        // Roll back an open transaction and persist, then acknowledge and leave without another line.
        const mv::Err rc = db.close();
        w.result(mv::Result(), id_json);
        std::fflush(stdout);
        mv::Prof::dump();
        return rc == mv::Err::Ok ? 0 : 1;
      }
      if (op_name != "execute") {
        w.protocol_error(id_json, "unsupported_op",
                         op_name.empty() ? "missing op" : "unsupported op: " + op_name);
        continue;
      }
      const mv::JsonValue* sql = mv::json_member(request, "sql");
      if (sql == nullptr || sql->kind != mv::JsonValue::Kind::Str) {
        w.protocol_error(id_json, "invalid_request", "missing sql string");
        continue;
      }
      // search_mode is accepted (exact or ann) and does not change any answer: this engine always
      // computes exact distances, which also exceeds the recall floor in ann mode.
      mv::Result r = db.execute(sql->s);
      w.result(r, id_json);
    }
    if (stop) break;
  }

  if (g_stop != 0) {
    std::fprintf(stderr, "signal received, shutting down cleanly\n");
  }
  db.close();
  mv::Prof::dump();
  return 0;
}
