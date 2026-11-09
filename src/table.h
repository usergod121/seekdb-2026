// One table: schema, row storage, primary key index and the vector arena.
//
// Rows are kept in a vector with a tombstone flag; the vector payload is stored out of line in a
// shared float arena so a scan reads contiguous memory (this is what the TopK kernel walks). The
// primary key index maps pk -> row slot, so a point lookup is one probe and duplicate keys are
// detected in O(1).
#pragma once

#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

#include "common.h"

namespace mv {

struct Table {
  std::string name;
  std::vector<Column> columns;
  int64_t pk_col = -1;
  int64_t vec_col = -1;      // -1 when the table has no VECTOR column

  // row storage
  std::vector<std::vector<Value>> rows;   // slot -> values (Value::vec_off points into arena)
  std::vector<uint8_t> alive;             // 1 = visible, 0 = deleted (tombstone)
  std::unordered_map<int64_t, int64_t, KeyHash> pk_index;   // pk -> slot
  std::vector<float> arena;               // float payload for all vectors
  int64_t arena_used = 0;                 // floats in use (arena.size() may be larger)

  // vector index (CREATE VECTOR INDEX): the task only requires that results stay correct after
  // inserts/updates/deletes, so the index is a monotonically bumped version plus a dense list of live
  // vector slots for scans. Exact search uses the arena directly; the structure exists so the DDL and
  // its maintenance semantics are real.
  bool has_vector_index = false;
  std::string vector_index_name;
  uint64_t index_version = 0;
  std::vector<int64_t> index_slots;

  int find_column(const std::string& name) const;
  int64_t dim() const { return (vec_col >= 0) ? columns[vec_col].len : 0; }

  // Allocates `n` floats in the arena and returns the offset (caller writes them).
  int64_t alloc_vector(int64_t n);
  void free_vector(int64_t off, int64_t n);

  void rebuild_index_slots();
};

}  // namespace mv
