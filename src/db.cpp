#include "db.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace mv {

std::string lower_ascii(const std::string& s) {
  std::string out = s;
  for (char& c : out) {
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
  }
  return out;
}

namespace {

constexpr const char* kWalName = "data.wal";
constexpr uint32_t kCatalogMagic = 0x4D564442;   // "MVDB"
constexpr uint32_t kCatalogVersion = 1;

bool write_all(const int fd, const void* buf, size_t n) {
  const char* p = static_cast<const char*>(buf);
  while (n > 0) {
    const ssize_t w = ::write(fd, p, n);
    if (w <= 0) return false;
    p += w;
    n -= static_cast<size_t>(w);
  }
  return true;
}

void put_u32(std::string* out, const uint32_t v) { out->append(reinterpret_cast<const char*>(&v), 4); }
void put_i64(std::string* out, const int64_t v) { out->append(reinterpret_cast<const char*>(&v), 8); }
void put_str(std::string* out, const std::string& s) {
  put_u32(out, static_cast<uint32_t>(s.size()));
  out->append(s);
}

struct Cursor {
  const char* p;
  size_t n;
  bool ok = true;
  uint32_t u32() {
    uint32_t v = 0;
    if (n < 4) { ok = false; return 0; }
    std::memcpy(&v, p, 4); p += 4; n -= 4; return v;
  }
  int64_t i64() {
    int64_t v = 0;
    if (n < 8) { ok = false; return 0; }
    std::memcpy(&v, p, 8); p += 8; n -= 8; return v;
  }
  std::string str() {
    const uint32_t len = u32();
    if (!ok || n < len) { ok = false; return std::string(); }
    std::string s(p, len);
    p += len; n -= len;
    return s;
  }
};

}  // namespace

Database::~Database() { (void)close(); }

std::string Database::table_pages_path(const std::string& table) const {
  return data_dir_ + "/t_" + lower_ascii(table) + ".pages";
}

// ---- open / close ---------------------------------------------------------

Err Database::open(const std::string& data_dir) {
  data_dir_ = data_dir;
  if (::mkdir(data_dir_.c_str(), 0755) != 0 && errno != EEXIST) return Err::IoError;
  catalog_path_ = data_dir_ + "/catalog.pages";
  wal_path_ = data_dir_ + "/" + kWalName;
  if (const char* po = ::getenv("MVDB_POOL_PAGES")) {
    const long v = std::strtol(po, nullptr, 10);
    if (v > 0) pool_pages_ = v;
  }
  if (catalog_pager_.open(catalog_path_, 8) != Err::Ok) return Err::IoError;

  if (catalog_pager_.page_count() == 0) {
    int32_t pid = -1;
    char* page = nullptr;
    if (catalog_pager_.alloc(&pid, &page) != Err::Ok || pid != 0) return Err::IoError;
    std::string buf;
    put_u32(&buf, kCatalogMagic);
    put_u32(&buf, kCatalogVersion);
    put_u32(&buf, 0);
    std::memcpy(page, buf.data(), buf.size());
    catalog_pager_.mark_dirty(0);
    if (catalog_pager_.flush() != Err::Ok) return Err::IoError;
  } else if (load_catalog() != Err::Ok) {
    return Err::IoError;
  }

  // Open each table's page file and rebuild its primary key index: recovery work happens here,
  // before the caller is allowed to report readiness.
  for (auto& kv : tables_) {
    Table& t = kv.second;
    if (t.open_pages(table_pages_path(t.name), pool_pages_) != Err::Ok) return Err::IoError;
    if (t.rebuild_pk_index() != Err::Ok) return Err::IoError;
  }

  wal_fd_ = ::open(wal_path_.c_str(), O_CREAT | O_RDWR | O_APPEND, 0644);
  if (wal_fd_ < 0) return Err::IoError;
  replaying_ = true;
  const Err rc = replay_wal();
  replaying_ = false;
  if (rc != Err::Ok) return rc;
  // Recovery ends with a checkpoint: the replayed state reaches the pages and the log is emptied, so a
  // crash during recovery never leaves work that has to be replayed twice.
  if (dirty_) {
    if (checkpoint() != Err::Ok) return Err::IoError;
  }
  return Err::Ok;
}

Err Database::close() {
  if (in_txn_) {
    (void)undo_apply_from(0, 0);
    in_txn_ = false;
    txn_log_.clear();
  }
  Err rc = Err::Ok;
  if (dirty_) rc = checkpoint();
  for (auto& kv : tables_) {
    (void)kv.second.close_pages();
  }
  (void)catalog_pager_.flush();
  (void)catalog_pager_.close();
  if (wal_fd_ >= 0) {
    ::close(wal_fd_);
    wal_fd_ = -1;
  }
  return rc;
}

// ---- catalog --------------------------------------------------------------

Err Database::save_catalog() {
  std::string buf;
  put_u32(&buf, kCatalogMagic);
  put_u32(&buf, kCatalogVersion);
  put_u32(&buf, static_cast<uint32_t>(tables_.size()));
  for (const auto& kv : tables_) {
    const Table& t = kv.second;
    put_str(&buf, t.name);
    put_i64(&buf, t.pk_col);
    put_i64(&buf, t.vec_col);
    put_u32(&buf, t.has_vector_index ? 1u : 0u);
    put_str(&buf, t.vector_index_name);
    put_u32(&buf, static_cast<uint32_t>(t.columns.size()));
    for (const Column& c : t.columns) {
      put_str(&buf, c.name);
      put_u32(&buf, static_cast<uint32_t>(c.type));
      put_u32(&buf, static_cast<uint32_t>(c.len));
      put_u32(&buf, c.is_primary ? 1u : 0u);
    }
  }
  if (buf.size() > static_cast<size_t>(kPageSize)) return Err::IoError;
  char* page = nullptr;
  if (catalog_pager_.get(0, &page) != Err::Ok) return Err::IoError;
  std::memset(page, 0, kPageSize);
  std::memcpy(page, buf.data(), buf.size());
  catalog_pager_.mark_dirty(0);
  return Err::Ok;
}

Err Database::load_catalog() {
  char* page = nullptr;
  if (catalog_pager_.get(0, &page) != Err::Ok) return Err::IoError;
  Cursor c{page, static_cast<size_t>(kPageSize), true};
  const uint32_t magic = c.u32();
  const uint32_t version = c.u32();
  if (!c.ok || magic != kCatalogMagic || version != kCatalogVersion) return Err::IoError;
  const uint32_t ntables = c.u32();
  for (uint32_t i = 0; i < ntables && c.ok; ++i) {
    Table t;
    t.name = c.str();
    t.pk_col = c.i64();
    t.vec_col = c.i64();
    t.has_vector_index = (c.u32() != 0);
    t.vector_index_name = c.str();
    const uint32_t ncols = c.u32();
    t.columns.resize(ncols);
    for (uint32_t ci = 0; ci < ncols && c.ok; ++ci) {
      t.columns[ci].name = c.str();
      t.columns[ci].type = static_cast<ColType>(c.u32());
      t.columns[ci].len = static_cast<int32_t>(c.u32());
      t.columns[ci].is_primary = (c.u32() != 0);
    }
    tables_[lower_ascii(t.name)] = std::move(t);
  }
  return c.ok ? Err::Ok : Err::IoError;
}

// ---- WAL ------------------------------------------------------------------

Err Database::wal_append(const std::string& sql) {
  if (wal_fd_ < 0) return Err::IoError;
  std::string rec;
  rec.reserve(sql.size() + 5);
  rec.push_back('S');
  const uint32_t len = static_cast<uint32_t>(sql.size());
  rec.append(reinterpret_cast<const char*>(&len), 4);
  rec += sql;
  return write_all(wal_fd_, rec.data(), rec.size()) ? Err::Ok : Err::IoError;
}

Err Database::wal_sync() {
  if (wal_fd_ < 0) return Err::IoError;
  return (::fdatasync(wal_fd_) == 0) ? Err::Ok : Err::IoError;
}

Err Database::wal_commit() {
  if (wal_fd_ < 0) return Err::IoError;
  const char kCommit = 'C';
  if (!write_all(wal_fd_, &kCommit, 1)) return Err::IoError;
  return wal_sync();
}

Err Database::checkpoint() {
  if (save_catalog() != Err::Ok) return Err::IoError;
  if (catalog_pager_.flush() != Err::Ok) return Err::IoError;
  for (auto& kv : tables_) {
    if (kv.second.pager.flush() != Err::Ok) return Err::IoError;
  }
  if (wal_fd_ >= 0) {
    ::close(wal_fd_);
    wal_fd_ = -1;
  }
  const int fd = ::open(wal_path_.c_str(), O_CREAT | O_TRUNC | O_RDWR | O_APPEND, 0644);
  if (fd < 0) return Err::IoError;
  ::fsync(fd);
  wal_fd_ = fd;
  dirty_ = false;
  return Err::Ok;
}

Err Database::replay_wal() {
  const int fd = ::open(wal_path_.c_str(), O_RDONLY);
  if (fd < 0) return Err::Ok;
  const off_t size = ::lseek(fd, 0, SEEK_END);
  if (size <= 0) { ::close(fd); return Err::Ok; }
  ::lseek(fd, 0, SEEK_SET);
  std::string buf(static_cast<size_t>(size), '\0');
  size_t got = 0;
  while (got < buf.size()) {
    const ssize_t r = ::read(fd, &buf[got], buf.size() - got);
    if (r <= 0) break;
    got += static_cast<size_t>(r);
  }
  ::close(fd);
  buf.resize(got);

  std::vector<std::string> pending;
  size_t pos = 0;
  bool changed = false;
  while (pos < buf.size()) {
    const char tag = buf[pos];
    if (tag == 'C') {
      ++pos;
      for (const std::string& sql : pending) {
        const Result r = execute(sql);
        // A statement whose effect is already in the pages (a checkpoint that was interrupted after
        // flushing them) is not an error during recovery: skipping it converges on the same state.
        if (r.err == Err::Ok) {
          changed = true;
        } else if (r.err != Err::DuplicateKey && r.err != Err::TableExists) {
          std::fprintf(stderr, "[recovery] skipped a log record: %s: %s\n", err_name(r.err),
                       r.message.c_str());
        }
      }
      pending.clear();
      continue;
    }
    if (tag != 'S' || pos + 5 > buf.size()) break;
    uint32_t len = 0;
    std::memcpy(&len, buf.data() + pos + 1, 4);
    const size_t begin = pos + 5;
    if (begin + len > buf.size()) break;
    pending.emplace_back(buf.substr(begin, len));
    pos = begin + len;
  }
  if (changed) dirty_ = true;
  return Err::Ok;
}

// ---- undo -----------------------------------------------------------------

void Database::undo_clear() {
  undo_rows_.clear();
  undo_ddl_.clear();
}

Err Database::undo_apply_from(const size_t row_mark, const size_t ddl_mark) {
  for (size_t idx = undo_rows_.size(); idx > row_mark; --idx) {
    const UndoRow& u = undo_rows_[idx - 1];
    Table* t = find_table(u.table);
    if (t == nullptr) continue;
    // The undo touches the heap directly, so the table's in-memory row cache has to go too: a SELECT
    // that ran inside the transaction may have built one over the pre-rollback state.
    t->invalidate_cache();
    auto it = t->pk_index.find(u.pk);
    if (!u.existed) {
      if (it != t->pk_index.end()) {
        (void)t->heap.remove(it->second);
        t->pk_index.erase(it);
        if (t->live_rows > 0) --t->live_rows;
      }
      continue;
    }
    if (it == t->pk_index.end()) {
      Rid rid;
      if (t->heap.insert(u.old_payload.data(), static_cast<int32_t>(u.old_payload.size()), &rid) ==
          Err::Ok) {
        t->pk_index[u.pk] = rid;
        ++t->live_rows;
      }
    } else {
      Rid new_rid;
      bool moved = false;
      if (t->heap.update(it->second, u.old_payload.data(),
                         static_cast<int32_t>(u.old_payload.size()), &new_rid, &moved) == Err::Ok) {
        t->pk_index[u.pk] = new_rid;
      }
    }
  }
  for (size_t idx = undo_ddl_.size(); idx > ddl_mark; --idx) {
    const UndoDdl& d = undo_ddl_[idx - 1];
    if (d.created_table) {
      tables_.erase(d.table);
      continue;
    }
    Table* t = find_table(d.table);
    if (t != nullptr && d.created_index) {
      t->has_vector_index = false;
      t->vector_index_name.clear();
    }
  }
  undo_rows_.resize(row_mark);
  undo_ddl_.resize(ddl_mark);
  return Err::Ok;
}

// ---- dispatch -------------------------------------------------------------

Table* Database::find_table(const std::string& name) {
  auto it = tables_.find(lower_ascii(name));
  return (it == tables_.end()) ? nullptr : &it->second;
}

Result Database::execute(const std::string& sql_text) {
  Result res;
  size_t b = 0;
  size_t e = sql_text.size();
  while (b < e && (sql_text[b] == ' ' || sql_text[b] == '\t' || sql_text[b] == '\r' ||
                   sql_text[b] == '\n')) {
    ++b;
  }
  while (e > b && (sql_text[e - 1] == ' ' || sql_text[e - 1] == '\t' || sql_text[e - 1] == '\r' ||
                   sql_text[e - 1] == '\n')) {
    --e;
  }
  if (b == e) {
    res.err = Err::SyntaxError;
    res.message = "empty statement";
    return res;
  }
  const std::string sql = sql_text.substr(b, e - b);

  Stmt st;
  std::string perr;
  const Err prc = parse_statement(sql, &st, &perr);
  if (prc != Err::Ok) {
    res.err = prc;
    res.message = perr;
    return res;
  }

  switch (st.kind) {
    case StmtKind::Begin:
      if (in_txn_) {
        res.err = Err::TxnBusy;
        res.message = "nested transaction";
        return res;
      }
      in_txn_ = true;
      undo_clear();
      txn_log_.clear();
      txn_ddl_ = false;
      return res;
    case StmtKind::Commit:
      if (in_txn_) {
        for (const std::string& s : txn_log_) {
          if (wal_append(s) != Err::Ok) { res.err = Err::IoError; return res; }
        }
        txn_log_.clear();
        if (wal_commit() != Err::Ok) { res.err = Err::IoError; return res; }
        in_txn_ = false;
        undo_clear();
        dirty_ = true;
        // A committed table definition must survive a crash, not only the next checkpoint.
        if (txn_ddl_ && save_catalog() != Err::Ok) {
          res.err = Err::IoError;
          return res;
        }
        txn_ddl_ = false;
      }
      return res;
    case StmtKind::Rollback:
      if (in_txn_) {
        (void)undo_apply_from(0, 0);
        txn_log_.clear();
        in_txn_ = false;
        txn_ddl_ = false;
        dirty_ = true;
      }
      return res;
    default:
      break;
  }

  const size_t row_mark = undo_rows_.size();
  const size_t ddl_mark = undo_ddl_.size();
  res = exec(st, /*replay=*/false);
  if (res.err != Err::Ok) {
    (void)undo_apply_from(row_mark, ddl_mark);
    return res;
  }
  const bool is_ddl = (st.kind == StmtKind::CreateTable || st.kind == StmtKind::CreateIndex);
  if (is_ddl) txn_ddl_ = true;
  if (st.kind != StmtKind::Select) {
    dirty_ = true;
    if (in_txn_) {
      txn_log_.push_back(sql);
    } else if (replaying_) {
      // The statement already came from the log; writing it back would duplicate every record.
    } else {
      if (wal_append(sql) != Err::Ok || wal_commit() != Err::Ok) {
        res.err = Err::IoError;
        return res;
      }
      // An auto-committed CREATE TABLE must be in the catalog before the statement is acknowledged,
      // otherwise a crash loses the definition while the page file may already hold rows for it.
      if (is_ddl && save_catalog() != Err::Ok) {
        res.err = Err::IoError;
        return res;
      }
      txn_ddl_ = false;
    }
  }
  return res;
}

Result Database::exec(const Stmt& st, const bool replay) {
  switch (st.kind) {
    case StmtKind::CreateTable: return exec_create_table(st, replay);
    case StmtKind::CreateIndex: return exec_create_index(st, replay);
    case StmtKind::Insert: return exec_insert(st, replay);
    case StmtKind::Select: return exec_select(st);
    case StmtKind::Update: return exec_update(st, replay);
    case StmtKind::Delete: return exec_delete(st, replay);
    default: break;
  }
  Result r;
  r.err = Err::NotSupported;
  return r;
}

}  // namespace mv
