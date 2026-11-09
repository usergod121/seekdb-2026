// The database: catalog, statement execution, transactions and durability.
//
// Durability model (snapshot + logical WAL):
//   * every statement that commits is appended to the WAL as its own SQL text, followed by a commit
//     marker; the WAL is fsynced before the commit is acknowledged;
//   * an explicit transaction buffers its statements and writes them with one commit marker at
//     COMMIT, so a crash before that leaves nothing to replay;
//   * a checkpoint writes the whole state to a snapshot file (temp + atomic rename) and truncates the
//     WAL, so recovery is "load snapshot, replay committed WAL records";
//   * replay applies statements idempotently (insert-if-absent, update-upsert, delete-if-present, DDL
//     if-absent), which makes replaying the same WAL twice safe.
#pragma once

#include <map>
#include <string>
#include <unordered_map>
#include <vector>

#include "common.h"
#include "sql/ast.h"
#include "table.h"

namespace mv {

struct Result {
  Err err = Err::Ok;
  std::string message;                            // error detail (stderr / diagnostics only)
  std::vector<std::string> col_names;
  std::vector<std::vector<std::string>> rows;     // already formatted text
  bool is_query = false;                          // SELECT-ish: rows are meaningful
};

class Database {
 public:
  Database() = default;

  // Opens (or creates) the data directory, loads the snapshot and replays the WAL. Must complete
  // before the caller reports "ready".
  Err open(const std::string& data_dir);
  // Clean shutdown: rolls back an open transaction, checkpoints and closes the WAL.
  Err close();

  Result execute(const std::string& sql);

 private:
  // --- execution ---
  Result exec(const Stmt& st, bool replay);
  Table* find_table(const std::string& name);
  Result exec_create_table(const Stmt& st, bool replay);
  Result exec_create_index(const Stmt& st, bool replay);
  Result exec_insert(const Stmt& st, bool replay);
  Result exec_select(const Stmt& st);
  Result exec_update(const Stmt& st, bool replay);
  Result exec_delete(const Stmt& st, bool replay);

  // --- durability ---
  Err wal_append(const std::string& sql);
  Err wal_commit();
  Err wal_sync();
  Err checkpoint();
  Err load_snapshot();
  Err save_snapshot();
  Err replay_wal();

  // --- transactions ---
  struct UndoRow {
    std::string table;   // lower-cased
    int64_t pk = 0;
    bool existed = false;
    std::vector<Value> old_row;
    std::vector<float> old_vec;   // payload of the old vector column, when present
  };
  struct UndoDdl {
    std::string table;
    bool created_table = false;
    bool created_index = false;
  };
  void undo_clear();
  Err undo_apply();
  // Rolls back only the effects recorded after the given marks. A failing statement uses this so that
  // previously successful statements of the same transaction survive.
  Err undo_apply_from(size_t row_mark, size_t ddl_mark);
  void capture_undo_insert(const std::string& table, const int64_t pk);

  std::string data_dir_;
  std::string snapshot_path_;
  std::string wal_path_;
  int wal_fd_ = -1;

  std::unordered_map<std::string, Table> tables_;   // key: lower-cased table name

  bool in_txn_ = false;
  std::vector<UndoRow> undo_rows_;
  std::vector<UndoDdl> undo_ddl_;
  std::vector<std::string> txn_log_;                // statements logged for the open transaction
  bool dirty_ = false;
};

// Lower-cases ASCII for case-insensitive identifier lookup; UTF-8 bytes are preserved.
std::string lower_ascii(const std::string& s);

}  // namespace mv
