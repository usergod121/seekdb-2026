#include "protocol/json.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace mv {

namespace {

struct Parser {
  const char* p;
  const char* end;
  std::string error;

  void skip_space() {
    while (p < end && (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n')) ++p;
  }

  bool fail(const char* what) {
    if (error.empty()) error = what;
    return false;
  }

  // Decodes one \uXXXX escape, writing UTF-8 (surrogate pairs handled).
  bool read_hex4(uint32_t* out) {
    if (end - p < 4) return fail("truncated \\u escape");
    uint32_t value = 0;
    for (int i = 0; i < 4; ++i) {
      const char c = p[i];
      value <<= 4;
      if (c >= '0' && c <= '9') {
        value |= static_cast<uint32_t>(c - '0');
      } else if (c >= 'a' && c <= 'f') {
        value |= static_cast<uint32_t>(c - 'a' + 10);
      } else if (c >= 'A' && c <= 'F') {
        value |= static_cast<uint32_t>(c - 'A' + 10);
      } else {
        return fail("bad \\u escape");
      }
    }
    p += 4;
    *out = value;
    return true;
  }

  static void append_utf8(std::string* out, const uint32_t cp) {
    if (cp < 0x80) {
      out->push_back(static_cast<char>(cp));
    } else if (cp < 0x800) {
      out->push_back(static_cast<char>(0xC0 | (cp >> 6)));
      out->push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp < 0x10000) {
      out->push_back(static_cast<char>(0xE0 | (cp >> 12)));
      out->push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
      out->push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else {
      out->push_back(static_cast<char>(0xF0 | (cp >> 18)));
      out->push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
      out->push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
      out->push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    }
  }

  bool read_string(std::string* out) {
    if (p >= end || *p != '"') return fail("expected a string");
    ++p;
    out->clear();
    while (p < end) {
      const unsigned char c = static_cast<unsigned char>(*p);
      if (c == '"') {
        ++p;
        return true;
      }
      if (c == '\\') {
        ++p;
        if (p >= end) return fail("truncated escape");
        switch (*p) {
          case '"': out->push_back('"'); ++p; break;
          case '\\': out->push_back('\\'); ++p; break;
          case '/': out->push_back('/'); ++p; break;
          case 'b': out->push_back('\b'); ++p; break;
          case 'f': out->push_back('\f'); ++p; break;
          case 'n': out->push_back('\n'); ++p; break;
          case 'r': out->push_back('\r'); ++p; break;
          case 't': out->push_back('\t'); ++p; break;
          case 'u': {
            ++p;
            uint32_t cp = 0;
            if (!read_hex4(&cp)) return false;
            if (cp >= 0xD800 && cp <= 0xDBFF && end - p >= 6 && p[0] == '\\' && p[1] == 'u') {
              const char* save = p;
              p += 2;
              uint32_t low = 0;
              if (read_hex4(&low) && low >= 0xDC00 && low <= 0xDFFF) {
                cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00);
              } else {
                p = save;
              }
            }
            append_utf8(out, cp);
            break;
          }
          default: return fail("bad escape");
        }
        continue;
      }
      if (c < 0x20) return fail("control character in string");
      out->push_back(static_cast<char>(c));
      ++p;
    }
    return fail("unterminated string");
  }

  bool read_number(JsonValue* out) {
    const char* start = p;
    if (p < end && (*p == '-' || *p == '+')) ++p;
    bool digits = false;
    while (p < end && *p >= '0' && *p <= '9') {
      digits = true;
      ++p;
    }
    bool is_double = false;
    if (p < end && *p == '.') {
      is_double = true;
      ++p;
      while (p < end && *p >= '0' && *p <= '9') {
        digits = true;
        ++p;
      }
    }
    if (p < end && (*p == 'e' || *p == 'E')) {
      is_double = true;
      ++p;
      if (p < end && (*p == '-' || *p == '+')) ++p;
      while (p < end && *p >= '0' && *p <= '9') ++p;
    }
    if (!digits) return fail("bad number");
    out->raw.assign(start, static_cast<size_t>(p - start));
    if (is_double) {
      out->kind = JsonValue::Kind::Double;
      out->d = std::strtod(out->raw.c_str(), nullptr);
      if (!std::isfinite(out->d)) return fail("non-finite number");
    } else {
      out->kind = JsonValue::Kind::Int;
      out->i = std::strtoll(out->raw.c_str(), nullptr, 10);
    }
    return true;
  }

  bool read_value(JsonValue* out, const int depth) {
    if (depth > 64) return fail("nesting too deep");
    skip_space();
    if (p >= end) return fail("unexpected end of input");
    switch (*p) {
      case '"': {
        out->kind = JsonValue::Kind::Str;
        const char* start = p;
        if (!read_string(&out->s)) return false;
        out->raw.assign(start, static_cast<size_t>(p - start));
        return true;
      }
      case '{': {
        out->kind = JsonValue::Kind::Object;
        ++p;
        skip_space();
        if (p < end && *p == '}') {
          ++p;
          return true;
        }
        for (;;) {
          skip_space();
          std::string key;
          if (!read_string(&key)) return false;
          skip_space();
          if (p >= end || *p != ':') return fail("expected ':'");
          ++p;
          JsonValue member;
          if (!read_value(&member, depth + 1)) return false;
          out->keys.push_back(std::move(key));
          out->items.push_back(std::move(member));
          skip_space();
          if (p < end && *p == ',') {
            ++p;
            continue;
          }
          if (p < end && *p == '}') {
            ++p;
            return true;
          }
          return fail("expected ',' or '}'");
        }
      }
      case '[': {
        out->kind = JsonValue::Kind::Array;
        ++p;
        skip_space();
        if (p < end && *p == ']') {
          ++p;
          return true;
        }
        for (;;) {
          JsonValue member;
          if (!read_value(&member, depth + 1)) return false;
          out->items.push_back(std::move(member));
          skip_space();
          if (p < end && *p == ',') {
            ++p;
            continue;
          }
          if (p < end && *p == ']') {
            ++p;
            return true;
          }
          return fail("expected ',' or ']'");
        }
      }
      case 't':
        if (end - p >= 4 && std::strncmp(p, "true", 4) == 0) {
          p += 4;
          out->kind = JsonValue::Kind::Bool;
          out->b = true;
          out->raw = "true";
          return true;
        }
        return fail("bad literal");
      case 'f':
        if (end - p >= 5 && std::strncmp(p, "false", 5) == 0) {
          p += 5;
          out->kind = JsonValue::Kind::Bool;
          out->b = false;
          out->raw = "false";
          return true;
        }
        return fail("bad literal");
      case 'n':
        if (end - p >= 4 && std::strncmp(p, "null", 4) == 0) {
          p += 4;
          out->kind = JsonValue::Kind::Null;
          out->raw = "null";
          return true;
        }
        return fail("bad literal");
      default:
        return read_number(out);
    }
  }
};

}  // namespace

bool json_parse(const std::string& text, JsonValue* out, std::string* err) {
  Parser parser;
  parser.p = text.data();
  parser.end = text.data() + text.size();
  Parser fresh;
  (void)fresh;
  if (!parser.read_value(out, 0)) {
    if (err != nullptr) *err = parser.error.empty() ? "invalid JSON" : parser.error;
    return false;
  }
  parser.skip_space();
  if (parser.p != parser.end) {
    if (err != nullptr) *err = "trailing content after JSON value";
    return false;
  }
  return true;
}

const JsonValue* json_member(const JsonValue& value, const char* key) {
  if (value.kind != JsonValue::Kind::Object) return nullptr;
  for (size_t i = 0; i < value.keys.size(); ++i) {
    if (value.keys[i] == key) return &value.items[i];
  }
  return nullptr;
}

void json_write_string(std::string* out, const std::string& text) {
  out->push_back('"');
  for (const unsigned char c : text) {
    switch (c) {
      case '"': out->append("\\\""); break;
      case '\\': out->append("\\\\"); break;
      case '\b': out->append("\\b"); break;
      case '\f': out->append("\\f"); break;
      case '\n': out->append("\\n"); break;
      case '\r': out->append("\\r"); break;
      case '\t': out->append("\\t"); break;
      default:
        if (c < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          out->append(buf);
        } else {
          out->push_back(static_cast<char>(c));
        }
    }
  }
  out->push_back('"');
}

void json_write_int(std::string* out, const int64_t value) {
  char buf[32];
  std::snprintf(buf, sizeof(buf), "%lld", static_cast<long long>(value));
  out->append(buf);
}

void json_write_double(std::string* out, const double value) {
  if (!std::isfinite(value)) {
    out->append("null");   // the protocol forbids NaN/Infinity
    return;
  }
  char buf[40];
  // Shortest representation that round-trips, so a distance prints as 2.5 and not 2.5000000000000004.
  for (int precision = 15; precision <= 17; ++precision) {
    std::snprintf(buf, sizeof(buf), "%.*g", precision, value);
    if (std::strtod(buf, nullptr) == value) break;
  }
  out->append(buf);
}

}  // namespace mv
