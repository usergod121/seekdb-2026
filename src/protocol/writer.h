// Wire format for results: the JSON-lines protocol published with the task package.
//
// One JSON object per line, flushed per message: the handshake is exactly {"type":"ready"}, a query
// answers with columns and rows, a modification with affected_rows, a failure with a non-empty
// error.code, and a shutdown answers {"id":N,"ok":true} after which nothing else may be printed.
#pragma once

#include <cstdio>
#include <string>

#include "db.h"

namespace mv {

class Writer {
 public:
  void set_out(FILE* f) { out_ = f; }

  // {"type":"ready"} - must be the first line and must be flushed before any request is read.
  void ready();
  // Writes one statement result and flushes. `id_json` is the request id exactly as it arrived, so its
  // JSON type is preserved.
  void result(const Result& r, const std::string& id_json);
  // A protocol-level failure that is not tied to a statement (bad JSON, unknown op).
  void protocol_error(const std::string& id_json, const char* code, const std::string& message);
  void note(const std::string& text);   // stderr diagnostics only

 private:
  void write_line(const std::string& line);
  FILE* out_ = stdout;
};

}  // namespace mv
