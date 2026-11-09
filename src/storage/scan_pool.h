// Persistent scan pool and the parallel TopK that uses it.
//
// A pool is created the first time it is needed and lives for the rest of the process: spawning threads
// per query would cost more than the scan. Workers park on a condition variable between queries, and
// the caller participates as worker 0, so a one-CPU machine never pays for a thread at all. Every worker
// filters its own slice and keeps its own bounded TopK ordered by (distance, primary key); the partial
// answers are merged with that same order, so the result never depends on how the range was split.
//
// Threads are created with pthreads rather than std::thread because the build has exceptions disabled:
// pthread_create reports failure through its return value, so a restricted environment simply ends up
// with fewer workers instead of a call to std::terminate.
#pragma once

#include <cstdint>
#include <vector>

namespace mv {

// One candidate as the fast path sees it.
struct VecHit {
  float dist = 0.0f;
  int64_t pk = 0;
  int64_t idx = -1;   // row index inside the cached column
};

// Predicate applied inside the workers, so filtering is spread over the scan instead of being a serial
// pass. `ctx` is whatever the caller passed; `row` is the row index in the cached column.
using KeepRowFn = bool (*)(const void* ctx, int64_t row);

// Number of workers the pool would use (1 when the machine reports a single CPU).
int scan_worker_count();

// Bounded TopK over `n` consecutive vectors at `base` against `q`. `pks` gives the primary key of each
// row and `keep`, when non-null, decides whether a row participates. The result is sorted by
// (distance, primary key) ascending and holds at most `k` entries.
void l2_topk_parallel(const float* base, const float* q, int64_t n, int64_t dims, int64_t k,
                      const int64_t* pks, KeepRowFn keep, const void* keep_ctx,
                      std::vector<VecHit>* out);

}  // namespace mv
