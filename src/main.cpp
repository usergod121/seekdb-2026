// Entry point: open the database, announce readiness, then serve statements read from stdin and write
// results to stdout. Logs go to stderr. No network listener is opened.
#include <csignal>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include <unistd.h>

#include "db.h"
#include "protocol/splitter.h"
#include "protocol/writer.h"

namespace {

volatile std::sig_atomic_t g_stop = 0;

// Trims surrounding whitespace and trailing CR of one input line.
std::string trim_line(const std::string& in) {
  size_t begin = 0;
  size_t end = in.size();
  while (begin < end && (in[begin] == ' ' || in[begin] == '\t' || in[begin] == '\r' || in[begin] == '\n')) {
    ++begin;
  }
  while (end > begin && (in[end - 1] == ' ' || in[end - 1] == '\t' || in[end - 1] == '\r' ||
                         in[end - 1] == '\n')) {
    --end;
  }
  return in.substr(begin, end - begin);
}

// A driver directive that arrived on the SQL channel. Recognising it means the driver always gets an
// answer instead of waiting for a statement that never produces output.
enum class Directive { None, Restart, ModeExact, ModeAnn, Shutdown };

Directive classify_directive(const std::string& line) {
  if (line == "--restart") return Directive::Restart;
  if (line == "--mode exact") return Directive::ModeExact;
  if (line == "--mode ann") return Directive::ModeAnn;
  // Graceful shutdown spellings a driver may send instead of a signal.
  if (line == "quit" || line == "exit" || line == "\\q" || line == "--quit" || line == "--exit" ||
      line == "--shutdown") {
    return Directive::Shutdown;
  }
  return Directive::None;
}

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

int main(int argc, char** argv) {
  std::string data_dir = "/data";
  if (!parse_args(argc, argv, &data_dir)) return 2;

  // Deliberately no SA_RESTART: a signal must interrupt the blocking read so shutdown happens promptly.
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
  w.ready();

  mv::Splitter splitter;
  std::vector<std::string> stmts;
  char buf[65536];
  bool eof = false;
  while (!eof && g_stop == 0) {
    const ssize_t n = ::read(STDIN_FILENO, buf, sizeof(buf));
    if (n < 0) {
      if (errno == EINTR) continue;   // signal: the loop condition handles shutdown
      break;
    }
    if (n == 0) {
      eof = true;
      break;
    }
    const std::string chunk(buf, static_cast<size_t>(n));
    // A directive usually arrives on its own line while the driver waits for a response.
    if (splitter.empty()) {
      const Directive directive = classify_directive(trim_line(chunk));
      if (directive == Directive::Shutdown) {
        std::fprintf(stderr, "shutdown command received\n");
        break;
      }
      if (directive != Directive::None) {
        // --restart is the driver's job (it closes and reopens the data directory); --mode only changes
        // how results may be produced, and this engine always answers exactly, so both are acknowledged.
        w.result(mv::Result());
        continue;
      }
    }
    stmts.clear();
    splitter.feed(chunk, &stmts);
    for (const std::string& sql : stmts) {
      if (g_stop != 0) break;
      // A statement consisting only of whitespace/comments produces no output.
      bool blank = true;
      for (char c : sql) {
        if (c != ' ' && c != '\t' && c != '\r' && c != '\n') { blank = false; break; }
      }
      if (blank) continue;
      const mv::Result r = db.execute(sql);
      w.result(r);
    }
  }

  // Clean shutdown: an open transaction is rolled back and the WAL is checkpointed by close().
  if (g_stop != 0) {
    std::fprintf(stderr, "signal received, shutting down cleanly\n");
  }
  db.close();
  w.bye();
  return 0;
}
