#include "protocol/writer.h"

namespace mv {

void Writer::ready() {
  std::fputs("READY\n", out_);
  std::fflush(out_);
}

void Writer::bye() {
  std::fputs("BYE\n", out_);
  std::fflush(out_);
}

void Writer::result(const Result& r) {
  if (r.err != Err::Ok) {
    std::fprintf(out_, "ERROR %s %s", err_name(r.err), err_text(r.err));
    if (!r.message.empty()) std::fprintf(out_, ": %s", r.message.c_str());
    std::fputc('\n', out_);
    std::fflush(out_);
    return;
  }
  if (r.is_query) {
    for (size_t i = 0; i < r.col_names.size(); ++i) {
      if (i != 0) std::fputc('\t', out_);
      std::fputs(r.col_names[i].c_str(), out_);
    }
    std::fputc('\n', out_);
    for (const std::vector<std::string>& row : r.rows) {
      for (size_t i = 0; i < row.size(); ++i) {
        if (i != 0) std::fputc('\t', out_);
        std::fputs(row[i].c_str(), out_);
      }
      std::fputc('\n', out_);
    }
    std::fputs("END\n", out_);
  } else {
    std::fprintf(out_, "OK %lld\n", static_cast<long long>(r.affected));
  }
  std::fflush(out_);
}

void Writer::note(const std::string& text) {
  std::fputs(text.c_str(), stderr);
  std::fputc('\n', stderr);
  std::fflush(stderr);
}

}  // namespace mv
