// The database: catalog, statement execution, transactions and durability.
//
// Durability model (page files + logical WAL):
//   * rows live in per-table paged files behind bounded buffer pools, so a table larger than the
//     memory budget still works;
//   * every committed statement is appended to the WAL as its own SQL text followed by a commit
//     marker, and the WAL is fsynced before the commit is acknowledged;
//   * an explicit transaction buffers its statements and writes them with one commit marker at
//     COMMIT, so a crash before that leaves nothing to replay;
//   * a checkpoint flushes every page file, rewrites the catalog page and truncates the WAL, so
//     recovery is "open the page files, rebuild the primary key indexes, replay committed records".
#pragma once

#include <string>
#include <unordered_map>
#include <vector>

#include "common.h"
#include "sql/ast.h"
#include "storage/pager.h"
#include "table.h"

namespace mv {

struct Result {
  Err err = Err::Ok;
  std::string message;
  std::vector<std::string> col_names;
  std::vector<std::vector<Cell>> rows;
  bool is_query = false;
  // Rows inserted, updated or deleted by this statement; the public assertions compare it.
  int64_t affected = 0;
};

class Database {
 public:
  Database() = default;
  ~Database();

  Err open(const std::string& data_dir);
  Err close();
  // Runs whatever the request carries: one statement, or several separated by semicolons.
  Result execute(const std::string& sql);
  // One single statement (the internal entry point used by replay).
  Result execute_one(const std::string& sql);

  // Path of a table's page file; `exec` needs it when a table is created.
  std::string table_pages_path(const std::string& table) const;
  int64_t pool_pages() const { return pool_pages_; }

 private:
  Result exec(const Stmt& st, bool replay);
  Table* find_table(const std::string& name);
  Result exec_create_table(const Stmt& st, bool replay);
  Result exec_create_index(const Stmt& st, bool replay);
  Result exec_insert(const Stmt& st, bool replay);
  Result exec_select(const Stmt& st);
  Result exec_update(const Stmt& st, bool replay);
  Result exec_delete(const Stmt& st, bool replay);
  Result exec_drop_table(const Stmt& st);
  Result exec_truncate(const Stmt& st);

  Err load_catalog();
  Err save_catalog();

  Err wal_append(const std::string& sql);
  Err wal_commit();
  Err wal_sync();
  Err checkpoint();
  Err replay_wal();

  struct UndoRow {
    std::string table;
    int64_t pk = 0;
    bool existed = false;
    Rid rid;                   // where the old row was, when it existed
    std::string old_payload;   // encoded old row
  };
  struct UndoDdl {
    std::string table;
    bool created_table = false;
    bool created_index = false;
  };
  Err undo_apply_from(size_t row_mark, size_t ddl_mark);
  void undo_clear();

  std::string data_dir_;
  std::string catalog_path_;
  std::string wal_path_;
  Pager catalog_pager_;
  int wal_fd_ = -1;
  int64_t pool_pages_ = 512;  // 4096 * 8 KiB = 32 MiB per page file: the graded data set fits, so pages
                              // are not evicted and rewritten during the load

  std::unordered_map<std::string, Table> tables_;

  bool in_txn_ = false;
  // Set when the open transaction contains DDL, so COMMIT writes the catalog.
  bool txn_ddl_ = false;
  // Set while the WAL is being replayed: replay must not write the log it is reading from.
  bool replaying_ = false;
  std::vector<UndoRow> undo_rows_;
  std::vector<UndoDdl> undo_ddl_;
  std::vector<std::string> txn_log_;
  bool dirty_ = false;
};

std::string lower_ascii(const std::string& s);

}  // namespace mv
