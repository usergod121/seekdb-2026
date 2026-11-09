// Wire format for results.
//
// PROVISIONAL: the exact result-set framing (separators, terminator, error text) is defined by the
// task package's test driver, which is not published yet. Everything that touches the format is kept
// in this one file so that adapting to the official protocol is a localized change.
#pragma once

#include <cstdio>
#include <string>

#include "db.h"

namespace mv {

class Writer {
 public:
  void set_out(FILE* f) { out_ = f; }

  void ready();
  void bye();
  // Writes one statement result and flushes, so a driver waiting on the pipe never stalls.
  void result(const Result& r);
  void note(const std::string& text);   // stderr diagnostics only

 private:
  FILE* out_ = stdout;
};

}  // namespace mv
