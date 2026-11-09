// Statement execution: DDL, INSERT, SELECT (including vector TopK), UPDATE and DELETE.
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>

#include "db.h"
#include "sql/lexer.h"

namespace mv {

namespace {

// Unicode code point count: VARCHAR(n) length semantics.
int64_t utf8_length(const std::string& s) {
  int64_t n = 0;
  for (unsigned char c : s) {
    if ((c & 0xC0) != 0x80) ++n;
  }
  return n;
}

// UTF-8 byte order equals Unicode code point order, so a byte compare implements the required order
// without any locale involvement.
int cmp_str(const std::string& a, const std::string& b) {
  const size_t n = std::min(a.size(), b.size());
  const int c = std::memcmp(a.data(), b.data(), n);
  if (c != 0) return c < 0 ? -1 : 1;
  if (a.size() == b.size()) return 0;
  return a.size() < b.size() ? -1 : 1;
}

struct Ctx {
  const Table* t = nullptr;
  const Row* row = nullptr;
};

bool eval_const_int(const Expr& e, int64_t* out) {
  if (e.kind == ExprKind::IntLit) {
    *out = e.i;
    return true;
  }
  if (e.kind == ExprKind::FloatLit) {
    *out = static_cast<int64_t>(e.f);
    return true;
  }
  return false;
}

// Parses a vector from a bare list [1,2,3] or its quoted form '[1,2,3]' - the task package fixes the
// spelling, so both are accepted.
bool vector_from_expr(const Expr& e, std::vector<float>* out) {
  if (e.kind == ExprKind::VectorLit) {
    *out = e.vec;
    return true;
  }
  if (e.kind != ExprKind::StrLit) return false;
  std::vector<Token> toks;
  std::string lerr;
  if (!tokenize(e.s, &toks, &lerr)) return false;
  size_t i = 0;
  if (i >= toks.size() || toks[i].kind != Tok::Punct || toks[i].text != "[") return false;
  ++i;
  if (i < toks.size() && toks[i].kind == Tok::Punct && toks[i].text == "]") {
    ++i;
  } else {
    for (;;) {
      if (i >= toks.size()) return false;
      double v = 0.0;
      if (toks[i].kind == Tok::Integer) {
        v = static_cast<double>(toks[i].i);
      } else if (toks[i].kind == Tok::Float) {
        v = toks[i].f;
      } else {
        return false;
      }
      out->push_back(static_cast<float>(v));
      ++i;
      if (i < toks.size() && toks[i].kind == Tok::Punct && toks[i].text == ",") { ++i; continue; }
      if (i < toks.size() && toks[i].kind == Tok::Punct && toks[i].text == "]") { ++i; break; }
      return false;
    }
  }
  return i + 1 == toks.size();   // only the terminating End token may follow
}

bool is_l2(const Expr& e) { return e.kind == ExprKind::Func && equals_ci(e.name, "L2_DISTANCE"); }

// L2 distance between the stored vector of the row and the query vector.
bool eval_l2(const Ctx& ctx, const Expr& fn, double* out) {
  if (fn.args.size() != 2) return false;
  const Expr& col = *fn.args[0];
  if (col.kind != ExprKind::Column) return false;
  std::vector<float> q;
  if (!vector_from_expr(*fn.args[1], &q)) return false;
  const int c = ctx.t->find_column(col.name);
  if (c < 0) return false;
  const int64_t dims = ctx.t->columns[static_cast<size_t>(c)].len;
  if (static_cast<int64_t>(q.size()) != dims) return false;
  if (static_cast<int64_t>(ctx.row->vec.size()) != dims) return false;
  const float* base = ctx.row->vec.data();
  double acc = 0.0;
  for (int64_t d = 0; d < dims; ++d) {
    const double diff = static_cast<double>(base[d]) - static_cast<double>(q[static_cast<size_t>(d)]);
    acc += diff * diff;
  }
  *out = std::sqrt(acc);
  return true;
}

struct SortKey {
  bool is_num = true;
  double num = 0.0;
  std::string str;
};

bool eval_scalar(const Ctx& ctx, const Expr& e, Value* out, SortKey* key, std::string* err) {
  switch (e.kind) {
    case ExprKind::IntLit:
      out->type = ColType::BigInt;
      out->i = e.i;
      if (key) { key->is_num = true; key->num = static_cast<double>(e.i); }
      return true;
    case ExprKind::FloatLit:
      out->type = ColType::BigInt;
      out->i = static_cast<int64_t>(e.f);
      if (key) { key->is_num = true; key->num = e.f; }
      return true;
    case ExprKind::StrLit:
      out->type = ColType::Varchar;
      out->s = e.s;
      if (key) { key->is_num = false; key->str = e.s; }
      return true;
    case ExprKind::Column: {
      const int c = ctx.t->find_column(e.name);
      if (c < 0) {
        if (err) *err = "unknown column " + e.name;
        return false;
      }
      *out = ctx.row->vals[static_cast<size_t>(c)];
      if (key) {
        if (out->type == ColType::Varchar) {
          key->is_num = false;
          key->str = out->s;
        } else if (out->type == ColType::BigInt) {
          key->is_num = true;
          key->num = static_cast<double>(out->i);
        } else {
          key->is_num = true;
          key->num = 0.0;
        }
      }
      return true;
    }
    case ExprKind::Func: {
      double d = 0.0;
      if (is_l2(e) && eval_l2(ctx, e, &d)) {
        out->type = ColType::BigInt;
        out->i = static_cast<int64_t>(d);
        if (key) { key->is_num = true; key->num = d; }
        return true;
      }
      if (err) *err = "unsupported function " + e.name;
      return false;
    }
    case ExprKind::VectorLit:
      if (err) *err = "vector literal is only valid in L2_DISTANCE / INSERT";
      return false;
    case ExprKind::BinOp:
      break;
  }
  if (err) *err = "unsupported expression";
  return false;
}

bool eval_bool(const Ctx& ctx, const Expr& e, bool* out, std::string* err) {
  if (e.kind != ExprKind::BinOp) {
    Value v;
    if (!eval_scalar(ctx, e, &v, nullptr, err)) return false;
    *out = (v.i != 0);
    return true;
  }
  if (equals_ci(e.name, "AND")) {
    bool a = false;
    bool b = false;
    if (!eval_bool(ctx, *e.args[0], &a, err)) return false;
    if (!a) { *out = false; return true; }
    if (!eval_bool(ctx, *e.args[1], &b, err)) return false;
    *out = b;
    return true;
  }
  SortKey kl;
  SortKey kr;
  Value l;
  Value r;
  if (!eval_scalar(ctx, *e.args[0], &l, &kl, err)) return false;
  if (!eval_scalar(ctx, *e.args[1], &r, &kr, err)) return false;
  int cmp = 0;
  if (kl.is_num && kr.is_num) {
    cmp = (kl.num < kr.num) ? -1 : (kl.num > kr.num ? 1 : 0);
  } else if (!kl.is_num && !kr.is_num) {
    cmp = cmp_str(kl.str, kr.str);
  } else {
    if (err) *err = "cannot compare a number with a string";
    return false;
  }
  if (equals_ci(e.name, "=")) *out = (cmp == 0);
  else if (equals_ci(e.name, "<")) *out = (cmp < 0);
  else if (equals_ci(e.name, "<=")) *out = (cmp <= 0);
  else if (equals_ci(e.name, ">")) *out = (cmp > 0);
  else if (equals_ci(e.name, ">=")) *out = (cmp >= 0);
  else if (equals_ci(e.name, "!=") || equals_ci(e.name, "<>")) *out = (cmp != 0);
  else {
    if (err) *err = "unsupported operator " + e.name;
    return false;
  }
  return true;
}

// Converts a literal expression into the stored form for one column. Vector payloads come back in
// `vec_out` (empty for non vector columns).
Err value_from_expr(const Expr& e, const Column& col, std::vector<float>* vec_out, Value* out) {
  switch (col.type) {
    case ColType::BigInt: {
      if (e.kind == ExprKind::IntLit) {
        out->type = ColType::BigInt;
        out->i = e.i;
        return Err::Ok;
      }
      if (e.kind == ExprKind::StrLit) {
        char* endp = nullptr;
        const long long v = std::strtoll(e.s.c_str(), &endp, 10);
        if (endp == nullptr || *endp != '\0') return Err::SyntaxError;
        out->type = ColType::BigInt;
        out->i = static_cast<int64_t>(v);
        return Err::Ok;
      }
      return Err::SyntaxError;
    }
    case ColType::Varchar: {
      if (e.kind == ExprKind::StrLit) {
        if (utf8_length(e.s) > col.len) return Err::ValueOverflow;
        out->type = ColType::Varchar;
        out->s = e.s;
        return Err::Ok;
      }
      if (e.kind == ExprKind::IntLit) {
        out->type = ColType::Varchar;
        out->s = std::to_string(e.i);
        if (utf8_length(out->s) > col.len) return Err::ValueOverflow;
        return Err::Ok;
      }
      return Err::SyntaxError;
    }
    case ColType::Vector: {
      std::vector<float> parsed;
      if (!vector_from_expr(e, &parsed)) return Err::SyntaxError;
      if (static_cast<int64_t>(parsed.size()) != col.len) return Err::VectorDimMismatch;
      for (float f : parsed) {
        if (!std::isfinite(f)) return Err::ValueOverflow;
      }
      out->type = ColType::Vector;
      out->vec_off = 0;
      *vec_out = std::move(parsed);
      return Err::Ok;
    }
  }
  return Err::NotSupported;
}

std::string fmt_double(const double v) {
  char buf[64];
  std::snprintf(buf, sizeof(buf), "%.6f", v);
  std::string s(buf);
  while (s.size() > 1 && s.back() == '0') s.pop_back();
  if (!s.empty() && s.back() == '.') s.pop_back();
  return s;
}

std::string fmt_value(const Value& v) {
  if (v.type == ColType::BigInt) return std::to_string(v.i);
  return v.s;
}

}  // namespace

// ---- CREATE ---------------------------------------------------------------

Result Database::exec_create_table(const Stmt& st, const bool replay) {
  Result r;
  const std::string key = lower_ascii(st.table);
  if (tables_.count(key) != 0) {
    if (!(replay || st.if_not_exists)) {
      r.err = Err::TableExists;
      r.message = "table already exists: " + st.table;
    }
    return r;
  }
  Table t;
  t.name = st.table;
  int pk_count = 0;
  int vec_count = 0;
  for (size_t i = 0; i < st.columns.size(); ++i) {
    const ColumnDef& d = st.columns[i];
    Column c;
    c.name = d.name;
    c.type = d.type;
    c.len = d.len;
    c.is_primary = d.primary;
    if (t.find_column(d.name) >= 0) {
      r.err = Err::ColumnExists;
      r.message = "duplicate column " + d.name;
      return r;
    }
    if (d.primary) {
      if (d.type != ColType::BigInt) {
        r.err = Err::NotSupported;
        r.message = "primary key must be BIGINT";
        return r;
      }
      t.pk_col = static_cast<int64_t>(i);
      ++pk_count;
    }
    if (d.type == ColType::Vector) {
      t.vec_col = static_cast<int64_t>(i);
      ++vec_count;
    }
    t.columns.push_back(std::move(c));
  }
  if (pk_count != 1) {
    r.err = Err::SyntaxError;
    r.message = "exactly one BIGINT primary key is required";
    return r;
  }
  if (vec_count > 1) {
    r.err = Err::NotSupported;
    r.message = "at most one VECTOR column per table";
    return r;
  }
  if (t.open_pages(table_pages_path(t.name), pool_pages_) != Err::Ok) {
    r.err = Err::IoError;
    r.message = "cannot open the table page file";
    return r;
  }
  tables_[key] = std::move(t);
  tables_[key].heap.rebind(&tables_[key].pager);
  UndoDdl d;
  d.table = key;
  d.created_table = true;
  undo_ddl_.push_back(d);
  return r;
}

Result Database::exec_create_index(const Stmt& st, const bool replay) {
  Result r;
  Table* t = find_table(st.table);
  if (t == nullptr) {
    r.err = Err::TableNotFound;
    r.message = "table doesn't exist: " + st.table;
    return r;
  }
  const int c = t->find_column(st.index_column);
  if (c < 0 || t->columns[static_cast<size_t>(c)].type != ColType::Vector) {
    r.err = Err::ColumnNotFound;
    r.message = "vector column not found: " + st.index_column;
    return r;
  }
  if (t->has_vector_index) {
    if (!replay) {
      r.err = Err::ColumnExists;
      r.message = "vector index already exists";
    }
    return r;
  }
  // The task requires results to stay correct after inserts, updates and deletes; an exact scan over
  // the stored vectors satisfies that by construction, so the index records only its own identity.
  t->has_vector_index = true;
  t->vector_index_name = st.index_name;
  UndoDdl d;
  d.table = lower_ascii(st.table);
  d.created_index = true;
  undo_ddl_.push_back(d);
  return r;
}

// ---- INSERT ---------------------------------------------------------------

Result Database::exec_insert(const Stmt& st, const bool replay) {
  Result r;
  Table* t = find_table(st.table);
  if (t == nullptr) {
    r.err = Err::TableNotFound;
    r.message = "table doesn't exist: " + st.table;
    return r;
  }
  const size_t ncols = t->columns.size();
  std::vector<int> order;
  if (st.insert_columns.empty()) {
    for (size_t i = 0; i < ncols; ++i) order.push_back(static_cast<int>(i));
  } else {
    for (const std::string& name : st.insert_columns) {
      const int c = t->find_column(name);
      if (c < 0) {
        r.err = Err::ColumnNotFound;
        r.message = "unknown column " + name;
        return r;
      }
      order.push_back(c);
    }
    if (order.size() != ncols) {
      r.err = Err::SyntaxError;
      r.message = "every column must be provided";
      return r;
    }
  }
  for (const std::vector<ExprPtr>& src : st.rows) {
    if (src.size() != order.size()) {
      r.err = Err::SyntaxError;
      r.message = "column count does not match value count";
      return r;
    }
    Row row;
    row.vals.assign(ncols, Value());
    for (size_t i = 0; i < order.size(); ++i) {
      const int c = order[i];
      Value v;
      std::vector<float> payload;
      const Err vrc = value_from_expr(*src[i], t->columns[static_cast<size_t>(c)], &payload, &v);
      if (vrc != Err::Ok) {
        r.err = vrc;
        r.message = "bad value for column " + t->columns[static_cast<size_t>(c)].name;
        return r;
      }
      if (!payload.empty()) row.vec = std::move(payload);
      row.vals[static_cast<size_t>(c)] = std::move(v);
    }
    const int64_t pk = row.vals[static_cast<size_t>(t->pk_col)].i;
    if (t->pk_index.count(pk) != 0) {
      if (replay) continue;   // idempotent replay
      r.err = Err::DuplicateKey;
      r.message = "duplicate primary key " + std::to_string(pk);
      return r;
    }
    std::string payload;
    if (t->encode_row(row, &payload) != Err::Ok) {
      r.err = Err::ValueOverflow;
      r.message = "row does not fit";
      return r;
    }
    UndoRow u;
    u.table = lower_ascii(t->name);
    u.pk = pk;
    u.existed = false;
    undo_rows_.push_back(std::move(u));
    Rid rid;
    if (t->heap.insert(payload.data(), static_cast<int32_t>(payload.size()), &rid) != Err::Ok) {
      r.err = Err::TooManyRows;
      r.message = "cannot place the row";
      return r;
    }
    t->pk_index[pk] = rid;
    ++t->live_rows;
  }
  return r;
}

// ---- SELECT ---------------------------------------------------------------

Result Database::exec_select(const Stmt& st) {
  Result r;
  r.is_query = true;
  Table* t = find_table(st.table);
  if (t == nullptr) {
    r.err = Err::TableNotFound;
    r.message = "table doesn't exist: " + st.table;
    return r;
  }
  if (st.projections.empty()) {
    for (const Column& c : t->columns) r.col_names.push_back(c.name);
  } else {
    for (size_t i = 0; i < st.projections.size(); ++i) {
      const Expr& e = *st.projections[i];
      const bool has_alias = (i < st.aliases.size() && !st.aliases[i].empty());
      r.col_names.push_back(has_alias ? st.aliases[i]
                                      : (e.kind == ExprKind::Column ? e.name : e.name));
    }
  }

  std::vector<Rid> rids;
  if (t->heap.live_rows(&rids) != Err::Ok) {
    r.err = Err::IoError;
    return r;
  }

  struct Cand {
    Rid rid;
    int64_t pk = 0;
    std::vector<SortKey> keys;
  };
  std::vector<Cand> cands;
  std::string perr;
  Row row;
  for (const Rid& rid : rids) {
    if (t->fetch_at(rid, &row) != Err::Ok) continue;
    Ctx ctx{t, &row};
    if (st.where) {
      bool keep = false;
      if (!eval_bool(ctx, *st.where, &keep, &perr)) {
        r.err = Err::SyntaxError;
        r.message = perr;
        return r;
      }
      if (!keep) continue;
    }
    Cand c;
    c.rid = rid;
    c.pk = row.vals[static_cast<size_t>(t->pk_col)].i;
    for (const OrderItem& it : st.order_by) {
      SortKey k;
      if (is_l2(*it.expr)) {
        double d = 0.0;
        if (!eval_l2(ctx, *it.expr, &d)) {
          r.err = Err::SyntaxError;
          r.message = "bad L2_DISTANCE arguments";
          return r;
        }
        k.is_num = true;
        k.num = d;
      } else {
        Value v;
        if (!eval_scalar(ctx, *it.expr, &v, &k, &perr)) {
          r.err = Err::SyntaxError;
          r.message = perr;
          return r;
        }
      }
      c.keys.push_back(std::move(k));
    }
    cands.push_back(std::move(c));
  }

  if (!st.order_by.empty()) {
    std::stable_sort(cands.begin(), cands.end(), [&](const Cand& a, const Cand& b) {
      for (size_t i = 0; i < st.order_by.size(); ++i) {
        const SortKey& ka = a.keys[i];
        const SortKey& kb = b.keys[i];
        int cmp = 0;
        if (ka.is_num && kb.is_num) {
          cmp = (ka.num < kb.num) ? -1 : (ka.num > kb.num ? 1 : 0);
        } else if (!ka.is_num && !kb.is_num) {
          cmp = cmp_str(ka.str, kb.str);
        }
        if (cmp != 0) return st.order_by[i].asc ? (cmp < 0) : (cmp > 0);
      }
      return a.pk < b.pk;   // mandated tie break
    });
  } else {
    // "未指�?ORDER BY 的结果按集合比较": emit in primary key order for determinism.
    std::stable_sort(cands.begin(), cands.end(),
                     [](const Cand& a, const Cand& b) { return a.pk < b.pk; });
  }

  int64_t limit = -1;
  if (st.limit && eval_const_int(*st.limit, &limit) && limit >= 0) {
    if (static_cast<int64_t>(cands.size()) > limit) cands.resize(static_cast<size_t>(limit));
  }

  r.rows.reserve(cands.size());
  for (const Cand& c : cands) {
    if (t->fetch_at(c.rid, &row) != Err::Ok) continue;
    Ctx ctx{t, &row};
    std::vector<std::string> line;
    if (st.projections.empty()) {
      for (size_t col = 0; col < t->columns.size(); ++col) {
        if (static_cast<int64_t>(col) == t->vec_col) {
          std::string s = "[";
          for (size_t d = 0; d < row.vec.size(); ++d) {
            if (d != 0) s.push_back(',');
            char buf[32];
            std::snprintf(buf, sizeof(buf), "%.6g", static_cast<double>(row.vec[d]));
            s += buf;
          }
          s.push_back(']');
          line.push_back(s);
        } else {
          line.push_back(fmt_value(row.vals[col]));
        }
      }
    } else {
      for (size_t i = 0; i < st.projections.size(); ++i) {
        const Expr& e = *st.projections[i];
        if (is_l2(e)) {
          double d = 0.0;
          if (!eval_l2(ctx, e, &d)) {
            r.err = Err::SyntaxError;
            r.message = "bad L2_DISTANCE arguments";
            return r;
          }
          line.push_back(fmt_double(d));
        } else {
          Value v;
          if (!eval_scalar(ctx, e, &v, nullptr, &perr)) {
            r.err = Err::SyntaxError;
            r.message = perr;
            return r;
          }
          line.push_back(fmt_value(v));
        }
      }
    }
    r.rows.push_back(std::move(line));
  }
  return r;
}

// ---- UPDATE / DELETE ------------------------------------------------------

Result Database::exec_update(const Stmt& st, const bool replay) {
  (void)replay;
  Result r;
  Table* t = find_table(st.table);
  if (t == nullptr) {
    r.err = Err::TableNotFound;
    r.message = "table doesn't exist: " + st.table;
    return r;
  }
  for (const auto& kv : st.assignments) {
    const int c = t->find_column(kv.first);
    if (c < 0) {
      r.err = Err::ColumnNotFound;
      r.message = "unknown column " + kv.first;
      return r;
    }
    if (c == t->pk_col) {
      r.err = Err::NotSupported;
      r.message = "primary key updates are not supported";
      return r;
    }
  }
  std::vector<Rid> rids;
  if (t->heap.live_rows(&rids) != Err::Ok) {
    r.err = Err::IoError;
    return r;
  }
  std::string perr;
  Row row;
  for (const Rid& rid : rids) {
    if (t->fetch_at(rid, &row) != Err::Ok) continue;
    Ctx ctx{t, &row};
    if (st.where) {
      bool keep = false;
      if (!eval_bool(ctx, *st.where, &keep, &perr)) {
        r.err = Err::SyntaxError;
        r.message = perr;
        return r;
      }
      if (!keep) continue;
    }
    std::string old_payload;
    if (t->encode_row(row, &old_payload) != Err::Ok) {
      r.err = Err::IoError;
      return r;
    }
    UndoRow u;
    u.table = lower_ascii(t->name);
    u.pk = row.vals[static_cast<size_t>(t->pk_col)].i;
    u.existed = true;
    u.rid = rid;
    u.old_payload = std::move(old_payload);
    undo_rows_.push_back(std::move(u));

    for (const auto& kv : st.assignments) {
      const int c = t->find_column(kv.first);
      Value v;
      std::vector<float> payload;
      const Err vrc = value_from_expr(*kv.second, t->columns[static_cast<size_t>(c)], &payload, &v);
      if (vrc != Err::Ok) {
        r.err = vrc;
        r.message = "bad value for column " + kv.first;
        return r;
      }
      if (!payload.empty()) row.vec = std::move(payload);
      row.vals[static_cast<size_t>(c)] = std::move(v);
    }
    std::string payload;
    if (t->encode_row(row, &payload) != Err::Ok) {
      r.err = Err::ValueOverflow;
      r.message = "row does not fit";
      return r;
    }
    Rid new_rid;
    bool moved = false;
    if (t->heap.update(rid, payload.data(), static_cast<int32_t>(payload.size()), &new_rid,
                       &moved) != Err::Ok) {
      r.err = Err::IoError;
      return r;
    }
    t->pk_index[u.pk] = new_rid;
  }
  return r;
}

Result Database::exec_delete(const Stmt& st, const bool replay) {
  (void)replay;
  Result r;
  Table* t = find_table(st.table);
  if (t == nullptr) {
    r.err = Err::TableNotFound;
    r.message = "table doesn't exist: " + st.table;
    return r;
  }
  std::vector<Rid> rids;
  if (t->heap.live_rows(&rids) != Err::Ok) {
    r.err = Err::IoError;
    return r;
  }
  std::string perr;
  Row row;
  for (const Rid& rid : rids) {
    if (t->fetch_at(rid, &row) != Err::Ok) continue;
    Ctx ctx{t, &row};
    if (st.where) {
      bool keep = false;
      if (!eval_bool(ctx, *st.where, &keep, &perr)) {
        r.err = Err::SyntaxError;
        r.message = perr;
        return r;
      }
      if (!keep) continue;
    }
    std::string old_payload;
    if (t->encode_row(row, &old_payload) != Err::Ok) {
      r.err = Err::IoError;
      return r;
    }
    UndoRow u;
    u.table = lower_ascii(t->name);
    u.pk = row.vals[static_cast<size_t>(t->pk_col)].i;
    u.existed = true;
    u.rid = rid;
    u.old_payload = std::move(old_payload);
    undo_rows_.push_back(std::move(u));

    if (t->heap.remove(rid) != Err::Ok) {
      r.err = Err::IoError;
      return r;
    }
    t->pk_index.erase(u.pk);
    if (t->live_rows > 0) --t->live_rows;
  }
  return r;
}

}  // namespace mv
