#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace mv {

enum class Tok {
  End,
  Ident,    // identifier or keyword (keywords are matched case-insensitively by the parser)
  Integer,
  Float,
  String,   // 'literal' with '' escaping
  Punct,    // one of ( ) , * = < <= > >= ; [ ]
};

struct Token {
  Tok kind = Tok::End;
  std::string text;   // payload for Ident/String; the operator text for Punct
  int64_t i = 0;      // Integer value
  double f = 0.0;     // Float value
  size_t pos = 0;     // byte offset in the input, for error messages
};

// Splits one statement (already trimmed of the trailing ';') into tokens. Returns false on a lexical
// error and fills *err with a short reason.
bool tokenize(const std::string& sql, std::vector<Token>* out, std::string* err);

bool equals_ci(const std::string& a, const char* b);

}  // namespace mv
