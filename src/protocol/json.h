// A small JSON reader/writer for the line protocol.
//
// The judge sends one JSON object per line and expects one JSON object per line back, so the only
// requirements are correctness (escapes, UTF-8 passthrough, exact integers) and compactness. The parser
// keeps the raw text of every scalar it was asked to remember, so a request id can be echoed back with
// exactly the JSON type it arrived with (a number must not come back as a string).
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace mv {

struct JsonValue {
  enum class Kind { Null, Bool, Int, Double, Str, Array, Object } kind = Kind::Null;
  bool b = false;
  int64_t i = 0;
  double d = 0.0;
  std::string s;                       // string payload, decoded to UTF-8
  std::string raw;                     // the scalar's original JSON text (used to echo ids verbatim)
  std::vector<JsonValue> items;        // array elements
  std::vector<std::string> keys;       // object keys, parallel to items
};

// Parses one complete JSON value. Returns false and fills `err` on malformed input.
bool json_parse(const std::string& text, JsonValue* out, std::string* err);

// Looks up a member of an object value; returns nullptr when absent or when `value` is not an object.
const JsonValue* json_member(const JsonValue& value, const char* key);

// Serialisation helpers: compact output, strings escaped, finite numbers only.
void json_write_string(std::string* out, const std::string& text);
void json_write_int(std::string* out, const int64_t value);
void json_write_double(std::string* out, const double value);

}  // namespace mv
