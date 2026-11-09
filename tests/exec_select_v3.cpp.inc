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
      r.col_names.push_back(has_alias ? st.aliases[i] : e.name);
    }
  }

  const int64_t dims = t->dim();

  // Resolve the query vectors once per statement: parsing the literal per row would dominate the scan.
  struct L2Spec {
    std::vector<float> q;
  };
  std::vector<L2Spec> order_l2(st.order_by.size());
  std::vector<L2Spec> proj_l2(st.projections.size());
  auto resolve_l2 = [&](const Expr& e, L2Spec* out) -> bool {
    if (!is_l2(e)) return true;
    if (e.args.size() != 2) return false;
    const Expr& col = *e.args[0];
    if (col.kind != ExprKind::Column) return false;
    const int c = t->find_column(col.name);
    if (c != static_cast<int>(t->vec_col) || dims <= 0) return false;
    std::vector<float> q;
    if (!vector_from_expr(*e.args[1], &q)) return false;
    if (static_cast<int64_t>(q.size()) != dims) return false;
    out->q = std::move(q);
    return true;
  };
  for (size_t i = 0; i < st.order_by.size(); ++i) {
    if (!resolve_l2(*st.order_by[i].expr, &order_l2[i])) {
      r.err = Err::SyntaxError;
      r.message = "bad L2_DISTANCE arguments";
      return r;
    }
  }
  for (size_t i = 0; i < st.projections.size(); ++i) {
    if (!resolve_l2(*st.projections[i], &proj_l2[i])) {
      r.err = Err::SyntaxError;
      r.message = "bad L2_DISTANCE arguments";
      return r;
    }
  }

  const bool cached = (t->build_cache() == Err::Ok);
  std::vector<Rid> rids;
  if (!cached && t->heap.live_rows(&rids) != Err::Ok) {
    r.err = Err::IoError;
    return r;
  }
  std::string perr;
  Row paged_row;

  // Builds one output line for a row.
  auto project = [&](const Row& row, const float* vec, std::vector<std::string>* line) -> bool {
    Ctx ctx{t, &row, vec};
    if (st.projections.empty()) {
      for (size_t col = 0; col < t->columns.size(); ++col) {
        if (static_cast<int64_t>(col) == t->vec_col) {
          std::string s = "[";
          for (int64_t d = 0; d < dims; ++d) {
            if (d != 0) s.push_back(',');
            char buf[32];
            std::snprintf(buf, sizeof(buf), "%.6g", static_cast<double>(vec[d]));
            s += buf;
          }
          s.push_back(']');
          line->push_back(s);
        } else {
          line->push_back(fmt_value(row.vals[col]));
        }
      }
      return true;
    }
    for (size_t i = 0; i < st.projections.size(); ++i) {
      const Expr& e = *st.projections[i];
      if (!proj_l2[i].q.empty()) {
        line->push_back(fmt_double(l2_distance(vec, proj_l2[i].q.data(), dims)));
      } else {
        Value v;
        if (!eval_scalar(ctx, e, &v, nullptr, &perr)) {
          r.err = Err::SyntaxError;
          r.message = perr;
          return false;
        }
        line->push_back(fmt_value(v));
      }
    }
    return true;
  };

  int64_t k_limit = -1;
  const bool have_limit = st.limit && eval_const_int(*st.limit, &k_limit) && k_limit >= 0;

  // ---- fast path: one ascending L2 ordering with a LIMIT -> bounded heap of size K ----
  if (cached && st.order_by.size() == 1 && !order_l2[0].q.empty() && st.order_by[0].asc &&
      have_limit) {
    const size_t k = static_cast<size_t>(k_limit);
    std::vector<HeapItem> heap;
    heap.reserve(k + 1);
    const float* q = order_l2[0].q.data();
    for (size_t i = 0; i < t->cache_rows.size(); ++i) {
      const Row& row = t->cache_rows[i];
      if (st.where) {
        Ctx ctx{t, &row, t->cache_vec(static_cast<int64_t>(i))};
        bool keep = false;
        if (!eval_bool(ctx, *st.where, &keep, &perr)) {
          r.err = Err::SyntaxError;
          r.message = perr;
          return r;
        }
        if (!keep) continue;
      }
      if (k == 0) continue;
      HeapItem item;
      item.dist = l2_distance(t->cache_vec(static_cast<int64_t>(i)), q, dims);
      item.pk = row.vals[static_cast<size_t>(t->pk_col)].i;
      item.cache_idx = static_cast<int64_t>(i);
      if (heap.size() < k) {
        heap.push_back(item);
        std::push_heap(heap.begin(), heap.end(), worse);
      } else if (worse(heap.front(), item)) {
        std::pop_heap(heap.begin(), heap.end(), worse);
        heap.back() = item;
        std::push_heap(heap.begin(), heap.end(), worse);
      }
    }
    std::sort(heap.begin(), heap.end(), [](const HeapItem& a, const HeapItem& b) {
      if (a.dist != b.dist) return a.dist < b.dist;
      return a.pk < b.pk;
    });
    r.rows.reserve(heap.size());
    for (const HeapItem& item : heap) {
      const Row& row = t->cache_rows[static_cast<size_t>(item.cache_idx)];
      std::vector<std::string> line;
      if (!project(row, t->cache_vec(item.cache_idx), &line)) return r;
      r.rows.push_back(std::move(line));
    }
    return r;
  }

  // ---- general path ----
  struct Cand {
    Rid rid;
    int64_t pk = 0;
    int64_t cache_idx = -1;
    std::vector<SortKey> keys;
  };
  std::vector<Cand> cands;

  auto consider = [&](const Row& row, const float* vec, const int64_t cache_idx,
                      const Rid& rid) -> bool {
    Ctx ctx{t, &row, vec};
    Cand c;
    c.rid = rid;
    c.cache_idx = cache_idx;
    c.pk = row.vals[static_cast<size_t>(t->pk_col)].i;
    if (st.where) {
      bool keep = false;
      if (!eval_bool(ctx, *st.where, &keep, &perr)) {
        r.err = Err::SyntaxError;
        r.message = perr;
        return false;
      }
      if (!keep) return true;
    }
    for (size_t i = 0; i < st.order_by.size(); ++i) {
      SortKey k;
      if (!order_l2[i].q.empty()) {
        k.is_num = true;
        k.num = l2_distance(vec, order_l2[i].q.data(), dims);
      } else {
        Value v;
        if (!eval_scalar(ctx, *st.order_by[i].expr, &v, &k, &perr)) {
          r.err = Err::SyntaxError;
          r.message = perr;
          return false;
        }
      }
      c.keys.push_back(std::move(k));
    }
    cands.push_back(std::move(c));
    return true;
  };

  if (cached) {
    for (size_t i = 0; i < t->cache_rows.size(); ++i) {
      Rid none;
      if (!consider(t->cache_rows[i], dims > 0 ? t->cache_vec(static_cast<int64_t>(i)) : nullptr,
                    static_cast<int64_t>(i), none)) {
        return r;
      }
    }
  } else {
    for (const Rid& rid : rids) {
      if (t->fetch_at(rid, &paged_row) != Err::Ok) continue;
      if (!consider(paged_row, dims > 0 ? paged_row.vec.data() : nullptr, -1, rid)) return r;
    }
  }
  if (r.err != Err::Ok) return r;

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
      return a.pk < b.pk;
    });
  } else {
    std::stable_sort(cands.begin(), cands.end(),
                     [](const Cand& a, const Cand& b) { return a.pk < b.pk; });
  }

  if (have_limit && static_cast<int64_t>(cands.size()) > k_limit) {
    cands.resize(static_cast<size_t>(k_limit));
  }

  r.rows.reserve(cands.size());
  for (const Cand& c : cands) {
    const Row* rowp = nullptr;
    const float* vecp = nullptr;
    if (c.cache_idx >= 0) {
      rowp = &t->cache_rows[static_cast<size_t>(c.cache_idx)];
      vecp = dims > 0 ? t->cache_vec(c.cache_idx) : nullptr;
    } else {
      if (t->fetch_at(c.rid, &paged_row) != Err::Ok) continue;
      rowp = &paged_row;
      vecp = dims > 0 ? paged_row.vec.data() : nullptr;
    }
    std::vector<std::string> line;
    if (!project(*rowp, vecp, &line)) return r;
    r.rows.push_back(std::move(line));
  }
  return r;
}

