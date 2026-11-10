#include <cstdio>
#include <cstring>
#include <utility>

#include "sql/ast.h"
#include "sql/lexer.h"

namespace mv {

namespace {

class Parser {
 public:
  Parser(const std::string& sql, std::vector<Token> toks, std::string* err)
      : sql_(sql), toks_(std::move(toks)), err_(err) {}

  Err parse(Stmt* out);

 private:
  const Token& cur() const { return toks_[pos_]; }
  const Token& peek(size_t k = 1) const {
    const size_t i = pos_ + k;
    return toks_[i < toks_.size() ? i : toks_.size() - 1];
  }
  bool at_end() const { return cur().kind == Tok::End; }
  void advance() { if (!at_end()) ++pos_; }

  bool is_kw(const char* kw) const { return cur().kind == Tok::Ident && equals_ci(cur().text, kw); }
  bool accept_kw(const char* kw) {
    if (!is_kw(kw)) return false;
    advance();
    return true;
  }
  bool expect_kw(const char* kw);
  bool is_punct(const char* p) const { return cur().kind == Tok::Punct && cur().text == p; }
  bool accept_punct(const char* p) {
    if (!is_punct(p)) return false;
    advance();
    return true;
  }
  bool expect_punct(const char* p);
  bool expect_ident(std::string* out);

  Err fail(Err code, const std::string& msg) {
    if (err_ != nullptr && err_->empty()) *err_ = msg;
    return code;
  }

  ExprPtr parse_expr();       // OR-less subset: AND over comparisons
  ExprPtr parse_and();
  ExprPtr parse_cmp();
  ExprPtr parse_primary();
  ExprPtr parse_value_list();  // [a, b, c] -> VectorLit

  Err parse_create(Stmt* out);
  Err parse_insert(Stmt* out);
  Err parse_select(Stmt* out);
  Err parse_update(Stmt* out);
  Err parse_delete(Stmt* out);

  const std::string& sql_;
  std::vector<Token> toks_;
  size_t pos_ = 0;
  std::string* err_;
};

bool Parser::expect_kw(const char* kw) {
  if (accept_kw(kw)) return true;
  if (err_ != nullptr) *err_ = std::string("expected keyword ") + kw;
  return false;
}

bool Parser::expect_punct(const char* p) {
  if (accept_punct(p)) return true;
  if (err_ != nullptr) *err_ = std::string("expected '") + p + "'";
  return false;
}

bool Parser::expect_ident(std::string* out) {
  if (cur().kind != Tok::Ident) {
    if (err_ != nullptr) *err_ = "expected identifier";
    return false;
  }
  *out = cur().text;
  advance();
  return true;
}

ExprPtr Parser::parse_value_list() {
  // cur() is '['; parse floats separated by ',' until ']'
  auto e = std::make_unique<Expr>();
  e->kind = ExprKind::VectorLit;
  advance();  // '['
  if (accept_punct("]")) return e;
  for (;;) {
    if (cur().kind != Tok::Integer && cur().kind != Tok::Float) {
      if (err_ != nullptr) *err_ = "vector element must be a number";
      return nullptr;
    }
    const double v = (cur().kind == Tok::Integer) ? static_cast<double>(cur().i) : cur().f;
    e->vec.push_back(static_cast<float>(v));
    advance();
    if (accept_punct(",")) continue;
    if (accept_punct("]")) break;
    if (err_ != nullptr) *err_ = "expected ',' or ']' in vector literal";
    return nullptr;
  }
  return e;
}

ExprPtr Parser::parse_primary() {
  if (is_punct("(")) {
    advance();
    ExprPtr inner = parse_expr();
    if (!expect_punct(")")) return nullptr;
    return inner;
  }
  if (is_punct("[")) return parse_value_list();
  if (cur().kind == Tok::Integer) {
    auto e = std::make_unique<Expr>();
    e->kind = ExprKind::IntLit;
    e->i = cur().i;
    advance();
    return e;
  }
  if (cur().kind == Tok::Float) {
    auto e = std::make_unique<Expr>();
    e->kind = ExprKind::FloatLit;
    e->f = cur().f;
    advance();
    return e;
  }
  if (cur().kind == Tok::String) {
    auto e = std::make_unique<Expr>();
    e->kind = ExprKind::StrLit;
    e->s = cur().text;
    advance();
    return e;
  }
  if (cur().kind == Tok::Ident) {
    const std::string name = cur().text;
    advance();
    if (accept_punct("(")) {
      auto e = std::make_unique<Expr>();
      e->kind = ExprKind::Func;
      e->name = name;
      for (char c : e->name) c = c;  // keep original case; compared case-insensitively later
      if (!accept_punct(")")) {
        for (;;) {
          ExprPtr a = parse_primary();
          if (a == nullptr) return nullptr;
          e->args.push_back(std::move(a));
          if (accept_punct(",")) continue;
          if (accept_punct(")")) break;
          if (err_ != nullptr) *err_ = "expected ',' or ')' in function call";
          return nullptr;
        }
      }
      return e;
    }
    auto e = std::make_unique<Expr>();
    e->kind = ExprKind::Column;
    e->name = name;
    return e;
  }
  if (err_ != nullptr) *err_ = "unexpected token in expression";
  return nullptr;
}

ExprPtr Parser::parse_cmp() {
  ExprPtr lhs = parse_primary();
  if (lhs == nullptr) return nullptr;
  const bool is_cmp = cur().kind == Tok::Punct &&
                      (cur().text == "=" || cur().text == "<" || cur().text == "<=" ||
                       cur().text == ">" || cur().text == ">=" || cur().text == "!=" ||
                       cur().text == "<>");
  if (!is_cmp) return lhs;
  auto e = std::make_unique<Expr>();
  e->kind = ExprKind::BinOp;
  e->name = cur().text;
  advance();
  ExprPtr rhs = parse_primary();
  if (rhs == nullptr) return nullptr;
  e->args.push_back(std::move(lhs));
  e->args.push_back(std::move(rhs));
  return e;
}

ExprPtr Parser::parse_and() {
  ExprPtr lhs = parse_cmp();
  if (lhs == nullptr) return nullptr;
  while (is_kw("AND")) {
    advance();
    ExprPtr rhs = parse_cmp();
    if (rhs == nullptr) return nullptr;
    auto e = std::make_unique<Expr>();
    e->kind = ExprKind::BinOp;
    e->name = "AND";
    e->args.push_back(std::move(lhs));
    e->args.push_back(std::move(rhs));
    lhs = std::move(e);
  }
  return lhs;
}

ExprPtr Parser::parse_expr() { return parse_and(); }

Err Parser::parse_create(Stmt* out) {
  // CREATE TABLE [IF NOT EXISTS] name (col type, ...)
  // CREATE VECTOR INDEX name ON table(col)
  if (accept_kw("TABLE")) {
    out->kind = StmtKind::CreateTable;
    if (accept_kw("IF")) {
      if (!expect_kw("NOT")) return Err::SyntaxError;
      if (!expect_kw("EXISTS")) return Err::SyntaxError;
      out->if_not_exists = true;
    }
    if (!expect_ident(&out->table)) return Err::SyntaxError;
    if (!expect_punct("(")) return Err::SyntaxError;
    for (;;) {
      ColumnDef c;
      if (!expect_ident(&c.name)) return Err::SyntaxError;
      if (is_kw("BIGINT") || is_kw("INT") || is_kw("INTEGER")) {
        advance();
        c.type = ColType::BigInt;
      } else if (is_kw("VARCHAR")) {
        advance();
        if (!expect_punct("(")) return Err::SyntaxError;
        if (cur().kind != Tok::Integer || cur().i <= 0) return fail(Err::SyntaxError, "VARCHAR length");
        c.type = ColType::Varchar;
        c.len = static_cast<int32_t>(cur().i);
        advance();
        if (!expect_punct(")")) return Err::SyntaxError;
      } else if (is_kw("VECTOR")) {
        advance();
        if (!expect_punct("(")) return Err::SyntaxError;
        if (cur().kind != Tok::Integer || cur().i <= 0) return fail(Err::SyntaxError, "VECTOR dim");
        c.type = ColType::Vector;
        c.len = static_cast<int32_t>(cur().i);
        advance();
        if (!expect_punct(")")) return Err::SyntaxError;
      } else {
        return fail(Err::NotSupported, "unsupported column type");
      }
      if (accept_kw("PRIMARY")) {
        if (!expect_kw("KEY")) return Err::SyntaxError;
        c.primary = true;
      }
      out->columns.push_back(std::move(c));
      if (accept_punct(",")) continue;
      if (accept_punct(")")) break;
      return fail(Err::SyntaxError, "expected ',' or ')' in column list");
    }
    return Err::Ok;
  }
  if (accept_kw("VECTOR")) {
    if (!expect_kw("INDEX")) return Err::SyntaxError;
    out->kind = StmtKind::CreateIndex;
    if (!expect_ident(&out->index_name)) return Err::SyntaxError;
    if (!expect_kw("ON")) return Err::SyntaxError;
    if (!expect_ident(&out->table)) return Err::SyntaxError;
    if (!expect_punct("(")) return Err::SyntaxError;
    if (!expect_ident(&out->index_column)) return Err::SyntaxError;
    if (!expect_punct(")")) return Err::SyntaxError;
    return Err::Ok;
  }
  return fail(Err::NotSupported, "only CREATE TABLE and CREATE VECTOR INDEX are supported");
}

Err Parser::parse_insert(Stmt* out) {
  // INSERT INTO t [(c1, c2, ...)] VALUES (.., ..), (.., ..)
  out->kind = StmtKind::Insert;
  if (!accept_kw("INTO")) return Err::SyntaxError;
  if (!expect_ident(&out->table)) return Err::SyntaxError;
  if (accept_punct("(")) {
    for (;;) {
      std::string c;
      if (!expect_ident(&c)) return Err::SyntaxError;
      out->insert_columns.push_back(std::move(c));
      if (accept_punct(",")) continue;
      if (accept_punct(")")) break;
      return fail(Err::SyntaxError, "expected ',' or ')' in insert column list");
    }
  }
  if (!expect_kw("VALUES")) return Err::SyntaxError;
  for (;;) {
    if (!expect_punct("(")) return Err::SyntaxError;
    std::vector<ExprPtr> row;
    for (;;) {
      ExprPtr v = parse_primary();
      if (v == nullptr) return Err::SyntaxError;
      row.push_back(std::move(v));
      if (accept_punct(",")) continue;
      if (accept_punct(")")) break;
      return fail(Err::SyntaxError, "expected ',' or ')' in VALUES");
    }
    out->rows.push_back(std::move(row));
    if (accept_punct(",")) continue;
    break;
  }
  return Err::Ok;
}

Err Parser::parse_select(Stmt* out) {
  out->kind = StmtKind::Select;
  if (accept_punct("*")) {
    // projections stay empty = '*'
  } else {
    for (;;) {
      ExprPtr e = parse_primary();
      if (e == nullptr) return Err::SyntaxError;
      // optional alias: AS name | name   (only after a plain column/function)
      std::string alias;
      if (accept_kw("AS")) {
        if (!expect_ident(&alias)) return Err::SyntaxError;
      }
      out->projections.push_back(std::move(e));
      out->aliases.push_back(std::move(alias));
      if (accept_punct(",")) continue;
      break;
    }
  }
  if (!expect_kw("FROM")) return Err::SyntaxError;
  if (!expect_ident(&out->table)) return Err::SyntaxError;
  if (accept_kw("WHERE")) {
    out->where = parse_expr();
    if (out->where == nullptr) return Err::SyntaxError;
  }
  if (accept_kw("ORDER")) {
    if (!expect_kw("BY")) return Err::SyntaxError;
    for (;;) {
      OrderItem it;
      it.expr = parse_primary();
      if (it.expr == nullptr) return Err::SyntaxError;
      if (accept_kw("DESC")) {
        it.asc = false;
      } else {
        accept_kw("ASC");
      }
      out->order_by.push_back(std::move(it));
      if (accept_punct(",")) continue;
      break;
    }
  }
  if (accept_kw("LIMIT")) {
    out->limit = parse_primary();
    if (out->limit == nullptr) return Err::SyntaxError;
    if (out->limit->kind == ExprKind::IntLit && out->limit->i < 0) {
      return fail(Err::SyntaxError, "negative LIMIT");
    }
  }
  return Err::Ok;
}

Err Parser::parse_update(Stmt* out) {
  out->kind = StmtKind::Update;
  if (!expect_ident(&out->table)) return Err::SyntaxError;
  if (!expect_kw("SET")) return Err::SyntaxError;
  for (;;) {
    std::string col;
    if (!expect_ident(&col)) return Err::SyntaxError;
    if (!expect_punct("=")) return Err::SyntaxError;
    ExprPtr v = parse_primary();
    if (v == nullptr) return Err::SyntaxError;
    out->assignments.emplace_back(std::move(col), std::move(v));
    if (accept_punct(",")) continue;
    break;
  }
  if (accept_kw("WHERE")) {
    out->where = parse_expr();
    if (out->where == nullptr) return Err::SyntaxError;
  }
  return Err::Ok;
}

Err Parser::parse_delete(Stmt* out) {
  out->kind = StmtKind::Delete;
  if (!expect_kw("FROM")) return Err::SyntaxError;
  if (!expect_ident(&out->table)) return Err::SyntaxError;
  if (accept_kw("WHERE")) {
    out->where = parse_expr();
    if (out->where == nullptr) return Err::SyntaxError;
  }
  return Err::Ok;
}

Err Parser::parse(Stmt* out) {
  if (at_end()) return fail(Err::SyntaxError, "empty statement");
  Err rc = Err::Ok;
  if (is_kw("CREATE")) {
    advance();
    rc = parse_create(out);
  } else if (is_kw("INSERT")) {
    advance();
    rc = parse_insert(out);
  } else if (is_kw("SELECT")) {
    advance();
    rc = parse_select(out);
  } else if (is_kw("UPDATE")) {
    advance();
    rc = parse_update(out);
  } else if (is_kw("DELETE")) {
    advance();
    rc = parse_delete(out);
  } else if (accept_kw("BEGIN")) {
    accept_kw("WORK");
    accept_kw("TRANSACTION");
    out->kind = StmtKind::Begin;
  } else if (accept_kw("START")) {
    if (!expect_kw("TRANSACTION")) return Err::SyntaxError;
    out->kind = StmtKind::Begin;
  } else if (accept_kw("COMMIT")) {
    out->kind = StmtKind::Commit;
  } else if (accept_kw("ROLLBACK")) {
    out->kind = StmtKind::Rollback;
  } else if (accept_kw("DROP")) {
    if (!expect_kw("TABLE")) return Err::SyntaxError;
    // DROP TABLE is not required by the task; report it as unsupported rather than guessing.
    return fail(Err::NotSupported, "DROP TABLE is not supported");
  } else {
    return fail(Err::NotSupported,
                std::string("unsupported statement: ") + cur().text);
  }
  if (rc != Err::Ok) return rc;
  if (!at_end()) return fail(Err::SyntaxError, std::string("trailing token: ") + cur().text);
  return Err::Ok;
}

}  // namespace

Err parse_statement(const std::string& sql, Stmt* out, std::string* err) {
  std::vector<Token> toks;
  std::string lex_err;
  if (!tokenize(sql, &toks, &lex_err)) {
    if (err != nullptr) *err = lex_err;
    // A literal that is well formed but does not fit the target type is a value range problem, not a
    // syntax one: "INSERT ... VALUES (9223372036854775808)" parses fine and then has nowhere to go.
    if (lex_err.find("out of range") != std::string::npos) return Err::ValueOverflow;
    return Err::SyntaxError;
  }
  Parser p(sql, std::move(toks), err);
  return p.parse(out);
}

}  // namespace mv
