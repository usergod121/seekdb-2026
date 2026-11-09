// One table: schema, rows in a private paged heap, primary key index and the row codec.
//
// Each table owns its own page file (data/t_<name>.pages) so its heap owns a contiguous page range
// and the buffer pool budget is per table. Rows live on disk pages; only the primary key index
// (pk -> (page, slot)) and one decoded row at a time are resident, so a table larger than the memory
// budget still works.
#pragma once

#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

#include "common.h"
#include "storage/heap.h"
#include "storage/pager.h"

namespace mv {

// A decoded row: the column values plus the vector payload of the vector column (if any).
struct Row {
  std::vector<Value> vals;
  std::vector<float> vec;
};

struct Table {
  std::string name;
  std::vector<Column> columns;
  int64_t pk_col = -1;
  int64_t vec_col = -1;

  Pager pager;
  Heap heap;
  int64_t live_rows = 0;

  std::unordered_map<int64_t, Rid, KeyHash> pk_index;

  bool has_vector_index = false;
  std::string vector_index_name;

  Err open_pages(const std::string& path, int64_t pool_pages);
  Err close_pages();

  int find_column(const std::string& name) const;
  int64_t dim() const { return (vec_col >= 0) ? columns[vec_col].len : 0; }

  // Row codec: for each column one type byte followed by its payload.
  Err encode_row(const Row& row, std::string* out) const;
  Err decode_row(const char* data, int32_t len, Row* out) const;

  Err fetch_at(const Rid& rid, Row* out);
  Err fetch(const int64_t pk, Row* out);
  // Rebuilds the primary key index from the heap (startup / recovery).
  Err rebuild_pk_index();
};

}  // namespace mv
