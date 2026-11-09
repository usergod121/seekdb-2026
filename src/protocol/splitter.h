// Statement splitting for a stream of SQL arriving on stdin: a statement ends at a ';' that is not
// inside a string literal or a comment. The remainder (an incomplete statement) stays in the buffer
// until more input arrives.
#pragma once

#include <string>
#include <vector>

namespace mv {

class Splitter {
 public:
  // Appends `chunk` to the pending buffer and moves every complete statement into `out`.
  void feed(const std::string& chunk, std::vector<std::string>* out);

  // True when only whitespace/comments are pending.
  bool empty() const;

  // Whatever is still buffered (used for diagnostics and for the "unterminated statement" case).
  const std::string& pending() const { return buf_; }

 private:
  std::string buf_;
};

}  // namespace mv
