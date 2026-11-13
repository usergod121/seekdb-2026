#include "sql/lexer.h"

#include <cctype>
#include <cerrno>
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
  // A token costs at least a few input bytes, so this bounds the number of reallocations for a large
  // statement without overshooting much on small ones.
  out->reserve(sql.size() / 8 + 8);
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
    if (c == '`') {
      // `quoted identifier`, the spelling MySQL tools emit; a doubled ` inside stands for one backtick
      std::string name;
      size_t j = i + 1;
      bool closed = false;
      while (j < n) {
        if (sql[j] == '`') {
          if (j + 1 < n && sql[j + 1] == '`') {
            name.push_back('`');
            j += 2;
            continue;
          }
          ++j;
          closed = true;
          break;
        }
        name.push_back(sql[j]);
        ++j;
      }
      if (!closed) {
        *err = "unterminated backtick identifier";
        return false;
      }
      if (name.empty()) {
        *err = "empty backtick identifier";
        return false;
      }
      t.kind = Tok::Ident;
      t.text = name;
      i = j;
      out->push_back(std::move(t));
      continue;
    }
    if (is_digit(c) ||
        ((c == '-' || c == '+') && i + 1 < n &&
         is_digit(static_cast<unsigned char>(sql[i + 1])))) {
      // number: integer or float (also used inside vector literals); a leading '+' is unary plus
      size_t j = i;
      const bool negative = (sql[j] == '-');
      if (sql[j] == '-' || sql[j] == '+') ++j;
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
        // strip a leading '+' so strtod sees a plain number
        const char* digits = (sql[i] == '+') ? t.text.c_str() + 1 : t.text.c_str();
        t.f = std::strtod(digits, nullptr);
        if (negative) t.f = -t.f;
      } else {
        t.kind = Tok::Integer;
        errno = 0;
        t.i = std::strtoll(t.text.c_str(), nullptr, 10);
        if (errno == ERANGE) {
          // Silently clamping would store a value the statement never asked for.
          *err = "integer literal out of range: " + t.text;
          return false;
        }
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
      // Fast path: scan to the closing quote and copy the run in one go. Any escape, doubled quote or
      // early end falls through to the general loop below, which produces the same payload.
      size_t scan = j;
      bool plain = true;
      while (scan < n) {
        const char sc = sql[scan];
        if (sc == quote) {
          if (scan + 1 < n && sql[scan + 1] == quote) {  // doubled quote: not the fast path
            plain = false;
            break;
          }
          break;
        }
        if (sc == '\\') {
          plain = false;
          break;
        }
        ++scan;
      }
      if (plain && scan < n && sql[scan] == quote) {
        t.kind = Tok::String;
        t.text.assign(sql, j, scan - j);
        i = scan + 1;
        out->push_back(std::move(t));
        continue;
      }
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
          // Known escapes follow MySQL; an unknown one keeps its backslash so the stored value is what the
          // statement wrote instead of something else.
          const char escaped = sql[j + 1];
          switch (escaped) {
            case '0': payload.push_back('\0'); break;
            case 'b': payload.push_back('\b'); break;
            case 'n': payload.push_back('\n'); break;
            case 'r': payload.push_back('\r'); break;
            case 't': payload.push_back('\t'); break;
            case 'Z': payload.push_back('\x1a'); break;
            case '\\':
            case '\'':
            case '"':
            case '%':
            case '_':
              payload.push_back(escaped);
              break;
            default:
              payload.push_back('\\');
              payload.push_back(escaped);
              break;
          }
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
        const char c1 = sql[i + 1];
        if ((c == '<' && (c1 == '=' || c1 == '>')) || (c == '>' && c1 == '=') ||
            (c == '!' && c1 == '=')) {
          t.kind = Tok::Punct;
          t.text.assign(sql, i, 2);
          i += 2;
          out->push_back(std::move(t));
          continue;
        }
      }
      static const char* kPunct = "(),.=*<>;[]";
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
