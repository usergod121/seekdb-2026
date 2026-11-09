#include "table.h"

#include "sql/lexer.h"

namespace mv {

int Table::find_column(const std::string& name) const {
  for (size_t i = 0; i < columns.size(); ++i) {
    if (equals_ci(columns[i].name, name.c_str())) return static_cast<int>(i);
  }
  return -1;
}

int64_t Table::alloc_vector(const int64_t n) {
  const int64_t off = arena_used;
  arena_used += n;
  if (static_cast<int64_t>(arena.size()) < arena_used) arena.resize(static_cast<size_t>(arena_used));
  return off;
}

void Table::free_vector(const int64_t off, const int64_t n) {
  (void)off;
  (void)n;
  // The arena is append only: a freed vector stays as dead space until the table is rewritten. This
  // keeps updates allocation-free in the common path and is fine for the workloads here; a table
  // rewrite (compaction) reclaims it.
}

void Table::rebuild_index_slots() {
  index_slots.clear();
  if (!has_vector_index) return;
  index_slots.reserve(rows.size());
  for (size_t slot = 0; slot < rows.size(); ++slot) {
    if (alive[slot] != 0 && vec_col >= 0 && rows[slot][vec_col].vec_off >= 0) {
      index_slots.push_back(static_cast<int64_t>(slot));
    }
  }
  ++index_version;
}

}  // namespace mv
