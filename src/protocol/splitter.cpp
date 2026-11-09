#include "protocol/splitter.h"

namespace mv {

void Splitter::feed(const std::string& chunk, std::vector<std::string>* out) {
  buf_ += chunk;
  if (start_of_stream_ && buf_.size() >= 3) {
    start_of_stream_ = false;
    if (static_cast<unsigned char>(buf_[0]) == 0xEF &&
        static_cast<unsigned char>(buf_[1]) == 0xBB &&
        static_cast<unsigned char>(buf_[2]) == 0xBF) {
      buf_.erase(0, 3);
    }
  }
  size_t start = 0;
  size_t i = 0;
  const size_t n = buf_.size();
  char quote = '\0';
  while (i < n) {
    const char c = buf_[i];
    if (quote != '\0') {
      if (c == '\\' && i + 1 < n) {
        i += 2;
        continue;
      }
      if (c == quote) {
        if (i + 1 < n && buf_[i + 1] == quote) {  // doubled quote
          i += 2;
          continue;
        }
        quote = '\0';
      }
      ++i;
      continue;
    }
    if (c == '\'' || c == '"') {
      quote = c;
      ++i;
      continue;
    }
    if (c == '-' && i + 1 < n && buf_[i + 1] == '-') {
      while (i < n && buf_[i] != '\n') ++i;
      continue;
    }
    if (c == '/' && i + 1 < n && buf_[i + 1] == '*') {
      i += 2;
      while (i + 1 < n && !(buf_[i] == '*' && buf_[i + 1] == '/')) ++i;
      i = (i + 1 < n) ? i + 2 : n;
      continue;
    }
    if (c == ';') {
      std::string stmt = buf_.substr(start, i - start);
      out->push_back(std::move(stmt));
      start = i + 1;
    }
    ++i;
  }
  if (start > 0) buf_.erase(0, start);
}

bool Splitter::empty() const {
  for (char c : buf_) {
    if (c != ' ' && c != '\t' && c != '\r' && c != '\n') return false;
  }
  return true;
}

}  // namespace mv
