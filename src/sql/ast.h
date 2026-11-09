// AST for the SQL subset the engine must support. Everything here is owned by the parse result and
// freed together, so plain vectors/strings are enough (no arena).
#pragma once

#include <memory>
#include <string>
#include <vector>

#include "common.h"

namespace mv {

enum class ExprKind {
  Column,      // column reference (by name)
  IntLit,
  FloatLit,
  StrLit,
  VectorLit,   // [1.0, 2.0, ...]
  Func,        // L2_DISTANCE(col, vector)
  BinOp,       // = < <= > >= AND
};

struct Expr;
using ExprPtr = std::unique_ptr<Expr>;

struct Expr {
  ExprKind kind = ExprKind::IntLit;
  std::string name;              // column name or function name (upper-cased) or operator text
  int64_t i = 0;
  double f = 0.0;
  std::string s;
  std::vector<float> vec;
  std::vector<ExprPtr> args;
};

enum class StmtKind { CreateTable, CreateIndex, Insert, Select, Update, Delete, Begin, Commit, Rollback };

struct ColumnDef {
  std::string name;
  ColType type = ColType::BigInt;
  int32_t len = 0;         // VARCHAR(n) characters | VECTOR(n) dims
  bool primary = false;
};

struct OrderItem {
  ExprPtr expr;
  bool asc = true;
};

struct Stmt {
  StmtKind kind = StmtKind::Select;

  // CREATE TABLE
  std::string table;
  bool if_not_exists = false;
  std::vector<ColumnDef> columns;
  std::string index_name;        // CREATE VECTOR INDEX index_name ON t(col)
  std::string index_column;

  // INSERT
  std::vector<std::vector<ExprPtr>> rows;
  std::vector<std::string> insert_columns;

  // SELECT / UPDATE / DELETE
  std::vector<ExprPtr> projections;   // empty means '*'
  std::vector<std::string> aliases;
  ExprPtr where;
  std::vector<OrderItem> order_by;
  ExprPtr limit;                      // integer literal expression
  std::vector<std::pair<std::string, ExprPtr>> assignments;  // UPDATE set
};

// Parses exactly one statement (the trailing ';' already stripped). On failure *err carries a short
// reason and Err::SyntaxError / Err::NotSupported is returned.
Err parse_statement(const std::string& sql, Stmt* out, std::string* err);

}  // namespace mv
