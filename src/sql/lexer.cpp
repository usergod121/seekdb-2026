#include "sql/lexer.h"

#include <cctype>
#include <cstdlib>
#include <cstring>

namespace mv {

bool equals_ci(const std::string& a, const char* b) {
  size_t i = 0;
  for (; i < a.size(); ++i) {
    if (b[i] == '\0') return false;
    const unsigned char ca = static_cast<unsigned char>(a[i]);
    const unsigned char cb = static_cast<unsigned char>(b[i]);
    if (std::tolower(ca) != std::tolower(cb)) return false;
  }
  return b[i] == '\0';
}

namespace {

bool is_digit(const char c) { return c >= '0' && c <= '9'; }

// Identifier characters: ASCII letters, digits, '_' and any byte >= 0x80 so that UTF-8 identifiers
// are carried through untouched (the engine compares them as bytes).
bool is_ident_char(const unsigned char c) {
  return std::isalnum(c) != 0 || c == '_' || c >= 0x80 ||
         std::memchr("$", c, 1) != nullptr;
}

}  // namespace

bool tokenize(const std::string& sql, std::vector<Token>* out, std::string* err) {
  out->clear();
  size_t i = 0;
  const size_t n = sql.size();
  while (i < n) {
    const unsigned char c = static_cast<unsigned char>(sql[i]);
    if (std::isspace(c)) {
      ++i;
      continue;
    }
    // comments: -- to end of line and /* ... */
    if (c == '-' && i + 1 < n && sql[i + 1] == '-') {
      while (i < n && sql[i] != '\n') ++i;
      continue;
    }
    if (c == '/' && i + 1 < n && sql[i + 1] == '*') {
      i += 2;
      while (i + 1 < n && !(sql[i] == '*' && sql[i + 1] == '/')) ++i;
      i = (i + 1 < n) ? i + 2 : n;
      continue;
    }
    Token t;
    t.pos = i;
    if (is_digit(c) || (c == '-' && i + 1 < n && is_digit(static_cast<unsigned char>(sql[i + 1])))) {
      // number: integer or float (also used inside vector literals)
      size_t j = i;
      if (sql[j] == '-') ++j;
      bool is_float = false;
      while (j < n && (is_digit(static_cast<unsigned char>(sql[j])) || sql[j] == '.' ||
                       sql[j] == 'e' || sql[j] == 'E' ||
                       ((sql[j] == '-' || sql[j] == '+') && j > i &&
                        (sql[j - 1] == 'e' || sql[j - 1] == 'E')))) {
        if (sql[j] == '.' || sql[j] == 'e' || sql[j] == 'E') is_float = true;
        ++j;
      }
      t.text = sql.substr(i, j - i);
      if (is_float) {
        t.kind = Tok::Float;
        t.f = std::strtod(t.text.c_str(), nullptr);
      } else {
        t.kind = Tok::Integer;
        t.i = std::strtoll(t.text.c_str(), nullptr, 10);
      }
      i = j;
    } else if (std::isalpha(c) || c == '_' || c >= 0x80) {
      size_t j = i;
      while (j < n && is_ident_char(static_cast<unsigned char>(sql[j]))) ++j;
      t.kind = Tok::Ident;
      t.text = sql.substr(i, j - i);
      i = j;
    } else if (c == '\'' || c == '"') {
      const char quote = static_cast<char>(c);
      size_t j = i + 1;
      std::string payload;
      bool closed = false;
      while (j < n) {
        if (sql[j] == quote) {
          if (j + 1 < n && sql[j + 1] == quote) {  // doubled quote escapes itself
            payload.push_back(quote);
            j += 2;
            continue;
          }
          closed = true;
          ++j;
          break;
        }
        if (sql[j] == '\\' && j + 1 < n) {
          payload.push_back(sql[j + 1]);
          j += 2;
          continue;
        }
        payload.push_back(sql[j]);
        ++j;
      }
      if (!closed) {
        *err = "unterminated string literal";
        return false;
      }
      t.kind = Tok::String;
      t.text = std::move(payload);
      i = j;
    } else {
      // punctuation; two character operators first
      if (i + 1 < n) {
        const std::string two = sql.substr(i, 2);
        if (two == "<=" || two == ">=" || two == "!=" || two == "<>") {
          t.kind = Tok::Punct;
          t.text = two;
          i += 2;
          out->push_back(std::move(t));
          continue;
        }
      }
      static const char* kPunct = "(),*=<>;[]";
      if (std::strchr(kPunct, static_cast<int>(c)) == nullptr) {
        *err = std::string("unexpected character '") + static_cast<char>(c) + "'";
        return false;
      }
      t.kind = Tok::Punct;
      t.text = std::string(1, static_cast<char>(c));
      ++i;
    }
    out->push_back(std::move(t));
  }
  Token end;
  end.kind = Tok::End;
  end.pos = n;
  out->push_back(std::move(end));
  return true;
}

}  // namespace mv
