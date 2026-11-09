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

  // Bounded in-memory cache of the whole table. When the data fits the budget a scan skips page I/O
  // and the vector payloads sit in one contiguous array, which is what lets the distance kernel
  // vectorise. When it does not fit, the paged path is used instead and correctness is unchanged.
  std::vector<Row> cache_rows;
  std::vector<int64_t> cache_pks;
  std::vector<float> cache_vecs;    // flat: row i occupies [i*dim(), (i+1)*dim())
  // Columnar copies of the scalar columns, so a WHERE clause reads a sequential array instead of
  // following one heap pointer per row through Row::vals. cache_str_off[c] has rows + 1 entries.
  std::vector<std::vector<int64_t>> cache_int_cols;
  std::vector<std::vector<char>> cache_str_data;
  std::vector<std::vector<uint32_t>> cache_str_off;
  bool cache_valid = false;
  // Rows are cached only while they fit this budget; beyond it every scan goes through the paged path.
  // MVDB_CACHE_BUDGET_BYTES overrides it so the paged path can be tested with a small data set.
  int64_t cache_budget_bytes = 64LL * 1024 * 1024;

  Err build_cache();
  void invalidate_cache() {
    cache_valid = false;
    cache_rows.clear();
    cache_pks.clear();
    cache_vecs.clear();
    cache_int_cols.clear();
    cache_str_data.clear();
    cache_str_off.clear();
  }
  const float* cache_vec(const int64_t idx) const { return cache_vecs.data() + idx * dim(); }

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
