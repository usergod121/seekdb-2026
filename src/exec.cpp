// Statement execution: DDL, INSERT, SELECT (including L2 TopK), UPDATE and DELETE.
#include <algorithm>
#include <cmath>
#include <cstring>

#include "db.h"
#include "sql/lexer.h"

namespace mv {

namespace {

// Unicode code point count for VARCHAR(n) length semantics.
int64_t utf8_length(const std::string& s) {
  int64_t n = 0;
  for (unsigned char c : s) {
    if ((c & 0xC0) != 0x80) ++n;
  }
  return n;
}

// UTF-8 byte order equals Unicode code point order, so a byte compare implements the required
// ordering without any locale involvement.
int cmp_str(const std::string& a, const std::string& b) {
  const size_t n = std::min(a.size(), b.size());
  const int c = std::memcmp(a.data(), b.data(), n);
  if (c != 0) return c < 0 ? -1 : 1;
  if (a.size() == b.size()) return 0;
  return a.size() < b.size() ? -1 : 1;
}

struct Ctx {
  Table* t = nullptr;
  const std::vector<Value>* row = nullptr;
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

// Parses a vector from either a bare list [1,2,3] or its quoted form '[1,2,3]'. The task package fixes
// the exact spelling, so both are accepted to stay compatible either way.
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
  return i + 1 == toks.size();  // only the terminating End token may follow
}

// L2 distance between the stored vector of `row` and the query vector.
bool eval_l2(const Table& t, const std::vector<Value>& row, const Expr& fn, double* out) {
  if (fn.args.size() != 2) return false;
  const Expr& col = *fn.args[0];
  if (col.kind != ExprKind::Column) return false;
  std::vector<float> q;
  if (!vector_from_expr(*fn.args[1], &q)) return false;
  const int c = t.find_column(col.name);
  if (c < 0) return false;
  const Value& v = row[static_cast<size_t>(c)];
  if (v.vec_off < 0) return false;
  const int64_t dims = t.columns[static_cast<size_t>(c)].len;
  if (static_cast<int64_t>(q.size()) != dims) return false;
  const float* base = t.arena.data() + v.vec_off;
  double acc = 0.0;
  for (int64_t d = 0; d < dims; ++d) {
    const double diff = static_cast<double>(base[d]) - static_cast<double>(q[static_cast<size_t>(d)]);
    acc += diff * diff;
  }
  *out = std::sqrt(acc);
  return true;
}

// A sort key: numeric or textual, plus the row's primary key for the mandated tie break.
struct SortKey {
  bool is_num = true;
  double num = 0.0;
  std::string str;
};

bool eval_scalar(const Ctx& ctx, const Expr& e, Value* out, SortKey* key, std::string* err);

bool eval_l2_expr(const Ctx& ctx, const Expr& e, double* out) {
  if (equals_ci(e.name, "L2_DISTANCE")) {
    return eval_l2(*ctx.t, *ctx.row, e, out);
  }
  return false;
}

bool eval_scalar(const Ctx& ctx, const Expr& e, Value* out, SortKey* key, std::string* err) {
  switch (e.kind) {
    case ExprKind::IntLit: {
      out->type = ColType::BigInt;
      out->i = e.i;
      if (key) { key->is_num = true; key->num = static_cast<double>(e.i); }
      return true;
    }
    case ExprKind::FloatLit: {
      out->type = ColType::BigInt;  // floats only appear as distances / literals
      out->i = static_cast<int64_t>(e.f);
      if (key) { key->is_num = true; key->num = e.f; }
      return true;
    }
    case ExprKind::StrLit: {
      out->type = ColType::Varchar;
      out->s = e.s;
      if (key) { key->is_num = false; key->str = e.s; }
      return true;
    }
    case ExprKind::Column: {
      const int c = ctx.t->find_column(e.name);
      if (c < 0) {
        if (err) *err = "unknown column " + e.name;
        return false;
      }
      *out = (*ctx.row)[static_cast<size_t>(c)];
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
      if (eval_l2_expr(ctx, e, &d)) {
        out->type = ColType::BigInt;
        out->i = static_cast<int64_t>(d);
        if (key) { key->is_num = true; key->num = d; }
        return true;
      }
      if (err) *err = "unsupported function " + e.name;
      return false;
    }
    case ExprKind::VectorLit: {
      if (err) *err = "vector literal is only valid in L2_DISTANCE / INSERT";
      return false;
    }
    case ExprKind::BinOp:
      break;
  }
  if (err) *err = "unsupported expression";
  return false;
}

// Distance for the current row if the expression is L2_DISTANCE, else false.
bool eval_distance(const Ctx& ctx, const Expr& e, double* out) {
  if (e.kind == ExprKind::Func) return eval_l2_expr(ctx, e, out);
  if (e.kind == ExprKind::Column) {
    // ORDER BY <alias>: the alias of a distance projection resolves to the same expression.
    return false;
  }
  return false;
}

bool value_truthy(const Value& v) { return v.i != 0; }

bool eval_bool(const Ctx& ctx, const Expr& e, bool* out, std::string* err) {
  if (e.kind == ExprKind::BinOp) {
    if (equals_ci(e.name, "AND")) {
      bool a = false, b = false;
      if (!eval_bool(ctx, *e.args[0], &a, err)) return false;
      if (!a) { *out = false; return true; }
      if (!eval_bool(ctx, *e.args[1], &b, err)) return false;
      *out = b;
      return true;
    }
    Value l, r;
    SortKey kl, kr;
    if (!eval_scalar(ctx, *e.args[0], &l, &kl, err)) {
      // allow a bare distance on either side
      double d = 0.0;
      if (!eval_distance(ctx, *e.args[0], &d)) return false;
      l.type = ColType::BigInt; l.i = static_cast<int64_t>(d);
      kl.is_num = true; kl.num = d;
    }
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
  Value v;
  if (!eval_scalar(ctx, e, &v, nullptr, err)) return false;
  *out = value_truthy(v);
  return true;
}

// Converts a literal expression to a stored value for the given column.
Err value_from_expr(const Expr& e, const Column& col, const int64_t dims_hint, std::vector<float>* vec_out,
                    Value* out) {
  switch (col.type) {
    case ColType::BigInt: {
      if (e.kind == ExprKind::IntLit) {
        out->type = ColType::BigInt;
        out->i = e.i;
        return Err::Ok;
      }
      if (e.kind == ExprKind::FloatLit) {
        return Err::SyntaxError;
      }
      if (e.kind == ExprKind::StrLit) {
        // a quoted integer is accepted only when it is exactly an integer
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
      // Accept both a bare list [1,2,3] and the quoted form '[1,2,3]': the task package fixes the
      // exact spelling, so both are honoured to stay compatible either way.
      std::vector<float> parsed;
      if (e.kind == ExprKind::VectorLit) {
        parsed = e.vec;
      } else if (e.kind == ExprKind::StrLit) {
        std::vector<Token> toks;
        std::string lerr;
        if (!tokenize(e.s, &toks, &lerr)) return Err::SyntaxError;
        size_t i = 0;
        if (i >= toks.size() || toks[i].kind != Tok::Punct || toks[i].text != "[") return Err::SyntaxError;
        ++i;
        if (i < toks.size() && toks[i].kind == Tok::Punct && toks[i].text == "]") {
          ++i;
        } else {
          for (;;) {
            if (i >= toks.size()) return Err::SyntaxError;
            double v = 0.0;
            if (toks[i].kind == Tok::Integer) {
              v = static_cast<double>(toks[i].i);
            } else if (toks[i].kind == Tok::Float) {
              v = toks[i].f;
            } else {
              return Err::SyntaxError;
            }
            parsed.push_back(static_cast<float>(v));
            ++i;
            if (i < toks.size() && toks[i].kind == Tok::Punct && toks[i].text == ",") { ++i; continue; }
            if (i < toks.size() && toks[i].kind == Tok::Punct && toks[i].text == "]") { ++i; break; }
            return Err::SyntaxError;
          }
        }
        if (i + 1 != toks.size()) return Err::SyntaxError;  // only the terminating End token may follow
      } else {
        return Err::SyntaxError;
      }
      if (static_cast<int64_t>(parsed.size()) != col.len) return Err::VectorDimMismatch;
      for (float f : parsed) {
        if (!std::isfinite(f)) return Err::ValueOverflow;
      }
      out->type = ColType::Vector;
      out->vec_off = -1;
      vec_out->assign(parsed.begin(), parsed.end());
      (void)dims_hint;
      return Err::Ok;
    }
  }
  return Err::NotSupported;
}

bool is_numeric_distance(const Expr& e) { return e.kind == ExprKind::Func && equals_ci(e.name, "L2_DISTANCE"); }

}  // namespace

// ---- CREATE TABLE / CREATE INDEX -----------------------------------------

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
  tables_[key] = std::move(t);
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
      r.err = Err::ColumnExists;  // an index already exists
      r.message = "vector index already exists";
    }
    return r;
  }
  t->has_vector_index = true;
  t->vector_index_name = st.index_name;
  t->rebuild_index_slots();
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
  // resolve the column order
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
    std::vector<Value> row(ncols);
    std::vector<float> vec_payload;
    int vec_target = -1;
    for (size_t i = 0; i < order.size(); ++i) {
      const int c = order[i];
      Value v;
      std::vector<float> payload;
      const Err vrc = value_from_expr(*src[i], t->columns[static_cast<size_t>(c)],
                                      t->columns[static_cast<size_t>(c)].len, &payload, &v);
      if (vrc != Err::Ok) {
        r.err = vrc;
        r.message = "bad value for column " + t->columns[static_cast<size_t>(c)].name;
        return r;
      }
      if (!payload.empty()) {
        vec_target = c;
        vec_payload = std::move(payload);
      }
      row[static_cast<size_t>(c)] = std::move(v);
    }
    const int64_t pk = row[static_cast<size_t>(t->pk_col)].i;
    auto found = t->pk_index.find(pk);
    if (found != t->pk_index.end()) {
      if (replay) continue;  // idempotent replay
      r.err = Err::DuplicateKey;
      r.message = "duplicate primary key " + std::to_string(pk);
      return r;
    }
    if (vec_target >= 0) {
      const int64_t dims = t->columns[static_cast<size_t>(vec_target)].len;
      const int64_t off = t->alloc_vector(dims);
      std::memcpy(t->arena.data() + off, vec_payload.data(), static_cast<size_t>(dims) * sizeof(float));
      row[static_cast<size_t>(vec_target)].vec_off = off;
    }
    UndoRow u;
    u.table = lower_ascii(t->name);
    u.pk = pk;
    u.existed = false;
    undo_rows_.push_back(std::move(u));
    t->pk_index[pk] = static_cast<int64_t>(t->rows.size());
    t->rows.push_back(std::move(row));
    t->alive.push_back(1);
    if (t->has_vector_index && vec_target >= 0) t->rebuild_index_slots();
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
  // Output columns
  struct OutCol {
    bool star = false;
    ExprPtr expr;      // not owned; points into st
    std::string alias;
  };
  std::vector<OutCol> outs;
  if (st.projections.empty()) {
    for (size_t c = 0; c < t->columns.size(); ++c) {
      OutCol oc;
      oc.star = true;
      oc.alias = t->columns[c].name;
      outs.push_back(std::move(oc));
    }
  } else {
    for (size_t i = 0; i < st.projections.size(); ++i) {
      OutCol oc;
      oc.expr = nullptr;
      oc.alias = (i < st.aliases.size() && !st.aliases[i].empty()) ? st.aliases[i] : "";
      outs.push_back(std::move(oc));
    }
  }
  for (size_t i = 0; i < outs.size(); ++i) {
    if (outs[i].star) continue;
    outs[i].expr = nullptr;  // resolved below from st.projections
  }
  r.col_names.reserve(outs.size());
  for (size_t i = 0; i < outs.size(); ++i) {
    if (st.projections.empty()) {
      r.col_names.push_back(t->columns[i].name);
    } else {
      const Expr& e = *st.projections[i];
      r.col_names.push_back(outs[i].alias.empty()
                                ? (e.kind == ExprKind::Column ? e.name : (e.kind == ExprKind::Func ? e.name : "expr"))
                                : outs[i].alias);
    }
  }

  // Collect the matching rows (slot ids) with their sort keys.
  struct Cand {
    int64_t slot;
    std::vector<SortKey> keys;
    std::vector<double> dists;   // distance per ORDER BY item when it is L2_DISTANCE
    int64_t pk;
  };
  std::vector<Cand> cands;
  std::string perr;
  for (size_t slot = 0; slot < t->rows.size(); ++slot) {
    if (t->alive[slot] == 0) continue;
    Ctx ctx{t, &t->rows[slot]};
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
    c.slot = static_cast<int64_t>(slot);
    c.pk = t->rows[slot][static_cast<size_t>(t->pk_col)].i;
    for (const OrderItem& it : st.order_by) {
      double d = 0.0;
      if (is_numeric_distance(*it.expr) && eval_distance(ctx, *it.expr, &d)) {
        SortKey k;
        k.is_num = true;
        k.num = d;
        c.keys.push_back(std::move(k));
        c.dists.push_back(d);
      } else {
        Value v;
        SortKey k;
        if (!eval_scalar(ctx, *it.expr, &v, &k, &perr)) {
          r.err = Err::SyntaxError;
          r.message = perr;
          return r;
        }
        c.keys.push_back(std::move(k));
        c.dists.push_back(0.0);
      }
    }
    cands.push_back(std::move(c));
  }

  if (!st.order_by.empty()) {
    // Order by the requested keys, tie-breaking on the primary key ascending as the task requires.
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
      return a.pk < b.pk;
    });
  } else {
    // "未指定 ORDER BY 的结果按集合比较": emit in primary key order so the output is deterministic.
    std::stable_sort(cands.begin(), cands.end(),
                     [](const Cand& a, const Cand& b) { return a.pk < b.pk; });
  }

  int64_t limit = -1;
  if (st.limit && eval_const_int(*st.limit, &limit) && limit >= 0) {
    if (static_cast<int64_t>(cands.size()) > limit) cands.resize(static_cast<size_t>(limit));
  }

  r.rows.reserve(cands.size());
  for (const Cand& c : cands) {
    Ctx ctx{t, &t->rows[static_cast<size_t>(c.slot)]};
    std::vector<std::string> line;
    line.reserve(outs.size());
    if (st.projections.empty()) {
      for (size_t col = 0; col < t->columns.size(); ++col) {
        const Value& v = (*ctx.row)[col];
        if (v.type == ColType::Vector) {
          std::string s = "[";
          for (int64_t d = 0; d < t->columns[col].len; ++d) {
            if (d != 0) s.push_back(',');
            char buf[32];
            std::snprintf(buf, sizeof(buf), "%.6g", static_cast<double>(t->arena[v.vec_off + d]));
            s += buf;
          }
          s.push_back(']');
          line.push_back(s);
        } else {
          line.push_back(v.type == ColType::BigInt ? std::to_string(v.i) : v.s);
        }
      }
    } else {
      for (size_t i = 0; i < st.projections.size(); ++i) {
        const Expr& e = *st.projections[i];
        if (is_numeric_distance(e)) {
          double d = 0.0;
          if (!eval_distance(ctx, e, &d)) {
            r.err = Err::SyntaxError;
            r.message = "bad L2_DISTANCE arguments";
            return r;
          }
          char buf[64];
          std::snprintf(buf, sizeof(buf), "%.6f", d);
          std::string s(buf);
          while (s.size() > 1 && s.back() == '0') s.pop_back();
          if (!s.empty() && s.back() == '.') s.pop_back();
          line.push_back(s);
        } else {
          Value v;
          SortKey k;
          if (!eval_scalar(ctx, e, &v, &k, &perr)) {
            r.err = Err::SyntaxError;
            r.message = perr;
            return r;
          }
          if (v.type == ColType::BigInt) {
            line.push_back(std::to_string(v.i));
          } else if (v.type == ColType::Varchar) {
            line.push_back(v.s);
          } else {
            line.push_back("[vector]");
          }
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
  std::string perr;
  for (size_t slot = 0; slot < t->rows.size(); ++slot) {
    if (t->alive[slot] == 0) continue;
    Ctx ctx{t, &t->rows[slot]};
    if (st.where) {
      bool keep = false;
      if (!eval_bool(ctx, *st.where, &keep, &perr)) {
        r.err = Err::SyntaxError;
        r.message = perr;
        return r;
      }
      if (!keep) continue;
    }
    // snapshot for rollback
    UndoRow u;
    u.table = lower_ascii(t->name);
    u.pk = t->rows[slot][static_cast<size_t>(t->pk_col)].i;
    u.existed = true;
    u.old_row = t->rows[slot];
    if (t->vec_col >= 0 && t->rows[slot][static_cast<size_t>(t->vec_col)].vec_off >= 0) {
      const int64_t dims = t->columns[static_cast<size_t>(t->vec_col)].len;
      u.old_vec.assign(t->arena.begin() + t->rows[slot][static_cast<size_t>(t->vec_col)].vec_off,
                       t->arena.begin() + t->rows[slot][static_cast<size_t>(t->vec_col)].vec_off + dims);
    }
    undo_rows_.push_back(std::move(u));
    bool vec_changed = false;
    std::vector<float> payload;
    for (const auto& kv : st.assignments) {
      const int c = t->find_column(kv.first);
      Value v;
      std::vector<float> p;
      const Err vrc = value_from_expr(*kv.second, t->columns[static_cast<size_t>(c)],
                                      t->columns[static_cast<size_t>(c)].len, &p, &v);
      if (vrc != Err::Ok) {
        r.err = vrc;
        r.message = "bad value for column " + kv.first;
        return r;
      }
      if (!p.empty()) {
        payload = std::move(p);
        vec_changed = true;
      }
      t->rows[slot][static_cast<size_t>(c)] = std::move(v);
    }
    if (vec_changed && t->vec_col >= 0) {
      const int64_t dims = t->columns[static_cast<size_t>(t->vec_col)].len;
      const int64_t off = t->alloc_vector(dims);
      std::memcpy(t->arena.data() + off, payload.data(), static_cast<size_t>(dims) * sizeof(float));
      t->rows[slot][static_cast<size_t>(t->vec_col)].vec_off = off;
    }
    if (t->has_vector_index) t->rebuild_index_slots();
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
  std::string perr;
  for (size_t slot = 0; slot < t->rows.size(); ++slot) {
    if (t->alive[slot] == 0) continue;
    Ctx ctx{t, &t->rows[slot]};
    if (st.where) {
      bool keep = false;
      if (!eval_bool(ctx, *st.where, &keep, &perr)) {
        r.err = Err::SyntaxError;
        r.message = perr;
        return r;
      }
      if (!keep) continue;
    }
    UndoRow u;
    u.table = lower_ascii(t->name);
    u.pk = t->rows[slot][static_cast<size_t>(t->pk_col)].i;
    u.existed = true;
    u.old_row = t->rows[slot];
    if (t->vec_col >= 0 && t->rows[slot][static_cast<size_t>(t->vec_col)].vec_off >= 0) {
      const int64_t dims = t->columns[static_cast<size_t>(t->vec_col)].len;
      u.old_vec.assign(t->arena.begin() + t->rows[slot][static_cast<size_t>(t->vec_col)].vec_off,
                       t->arena.begin() + t->rows[slot][static_cast<size_t>(t->vec_col)].vec_off + dims);
    }
    undo_rows_.push_back(std::move(u));
    t->alive[slot] = 0;
    t->pk_index.erase(t->rows[slot][static_cast<size_t>(t->pk_col)].i);
    if (t->has_vector_index) t->rebuild_index_slots();
  }
  return r;
}

}  // namespace mv
