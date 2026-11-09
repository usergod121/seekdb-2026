// Shared vocabulary for the little engine: values, status/error codes and the schema descriptors.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace mv {

// Error codes. The judge's exact codes come with the task package; this enum is the internal
// classification and is mapped to the wire codes in exactly one place (protocol/writer.cpp).
enum class Err {
  Ok = 0,
  SyntaxError,
  NotSupported,
  TableNotFound,
  TableExists,
  ColumnNotFound,
  ColumnExists,
  DuplicateKey,
  VectorDimMismatch,
  ValueOverflow,
  IoError,
  TxnBusy,
  TooManyRows,
};

inline const char* err_text(Err e) {
  switch (e) {
    case Err::Ok: return "OK";
    case Err::SyntaxError: return "syntax error";
    case Err::NotSupported: return "not supported";
    case Err::TableNotFound: return "table doesn't exist";
    case Err::TableExists: return "table already exists";
    case Err::ColumnNotFound: return "unknown column";
    case Err::ColumnExists: return "duplicate column";
    case Err::DuplicateKey: return "duplicate primary key";
    case Err::VectorDimMismatch: return "vector dimension mismatch";
    case Err::ValueOverflow: return "value out of range";
    case Err::IoError: return "io error";
    case Err::TxnBusy: return "transaction already open";
    case Err::TooManyRows: return "too many rows";
  }
  return "unknown";
}

enum class ColType { BigInt, Varchar, Vector };

struct Column {
  std::string name;
  ColType type = ColType::BigInt;
  int32_t len = 0;      // VARCHAR(n) in Unicode characters, VECTOR(n) in dimensions
  bool is_primary = false;
};

// A single cell. Vectors live in a shared arena so a row scan stays contiguous and cheap to copy.
struct Value {
  ColType type = ColType::BigInt;
  int64_t i = 0;
  std::string s;             // VARCHAR payload (UTF-8); length semantics are checked by the binder
  int64_t vec_off = -1;      // offset into the owning table's float arena, -1 when NULL
};

struct KeyHash {
  size_t operator()(const int64_t v) const {
    uint64_t x = static_cast<uint64_t>(v);
    x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33;
    return static_cast<size_t>(x);
  }
};

}  // namespace mv
