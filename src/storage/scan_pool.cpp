// Persistent scan pool and the parallel TopK that uses it. See scan_pool.h for the contract.
#include "storage/scan_pool.h"

#include <pthread.h>
#include <sched.h>
#include <unistd.h>

#include <algorithm>
#include <cmath>
#include <chrono>
#include <limits>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>

#if defined(__x86_64__) || defined(_M_X64)
#include <immintrin.h>
#define MV_X86 1
#endif

#include "storage/vec_kernel.h"

namespace mv {

namespace {

// Rows per block inside a worker: small enough to stay in cache, large enough to amortise the call.
constexpr int64_t kBlockRows = 256;
// Below this many comparisons the pool is not worth waking up.
constexpr int64_t kParallelThreshold = 1 << 17;
// Threads past this point add contention instead of throughput on the machines this targets.
constexpr int kMaxWorkers = 4;

// How many CPUs this process may actually use: sysconf reports every CPU on the host even when the
// container is pinned by an affinity mask or capped by a cgroup CPU quota, and starting more workers
// than that only adds contention.
int usable_cpu_count() {
  long cpus = sysconf(_SC_NPROCESSORS_ONLN);
  int count = (cpus <= 0) ? 1 : static_cast<int>(cpus);
  cpu_set_t set;
  CPU_ZERO(&set);
  if (sched_getaffinity(0, sizeof(set), &set) == 0) {
    const int allowed = CPU_COUNT(&set);
    if (allowed > 0 && allowed < count) count = allowed;
  }
  if (FILE* f = std::fopen("/sys/fs/cgroup/cpu.max", "re")) {
    char quota[32] = {0};
    char period[32] = {0};
    if (std::fscanf(f, "%31s %31s", quota, period) == 2 && std::strcmp(quota, "max") != 0) {
      const long long q = std::atoll(quota);
      const long long p = std::atoll(period);
      if (q > 0 && p > 0) {
        const int allowance = static_cast<int>((q + p - 1) / p);
        if (allowance >= 1 && allowance < count) count = allowance;
      }
    }
    std::fclose(f);
  }
  return count < 1 ? 1 : count;
}

inline bool ranks_before(const VecHit& a, const VecHit& b) {
  if (a.dist != b.dist) return a.dist < b.dist;
  return a.pk < b.pk;
}

// Distance scratch, reused across calls: allocating and zeroing a fresh block per call costs more than
// the scan once the work is split across workers.
float* block_scratch(const size_t need) {
  static thread_local std::vector<float> buffer;
  if (buffer.size() < need) buffer.resize(need);
  return buffer.data();
}

// Collects the positions whose value is at or below the limit. This is the filter that keeps rows out of
// the TopK heap: at k=10 over 10000 rows fewer than a tenth of a percent survive, so touching the heap per
// row was more expensive than computing the distances.
int64_t filter_le_scalar(const float* values, const int64_t count, const float limit, int32_t* out) {
  int64_t n = 0;
  for (int64_t i = 0; i < count; ++i) {
    if (values[i] <= limit) out[n++] = static_cast<int32_t>(i);
  }
  return n;
}

#if defined(MV_X86)
__attribute__((target("avx2"))) int64_t filter_le_avx2(const float* values, const int64_t count,
                                                       const float limit, int32_t* out) {
  int64_t n = 0;
  int64_t i = 0;
  const __m256 bound = _mm256_set1_ps(limit);
  for (; i + 8 <= count; i += 8) {
    const __m256 block = _mm256_loadu_ps(values + i);
    int mask = _mm256_movemask_ps(_mm256_cmp_ps(block, bound, _CMP_LE_OQ));
    while (mask != 0) {
      const int lane = __builtin_ctz(static_cast<unsigned>(mask));
      mask &= mask - 1;
      out[n++] = static_cast<int32_t>(i + lane);
    }
  }
  for (; i < count; ++i) {
    if (values[i] <= limit) out[n++] = static_cast<int32_t>(i);
  }
  return n;
}
#endif

int64_t filter_le(const float* values, const int64_t count, const float limit, int32_t* out) {
#if defined(MV_X86)
  if (__builtin_cpu_supports("avx2")) return filter_le_avx2(values, count, limit, out);
#endif
  return filter_le_scalar(values, count, limit, out);
}

// Bounded TopK of one worker: unsorted storage plus the position of the element that ranks last.
struct TopK {
  int64_t k = 0;
  std::vector<VecHit> items;
  size_t worst_at = 0;

  explicit TopK(const int64_t limit) : k(limit > 0 ? limit : 0) {}

  bool full() const { return static_cast<int64_t>(items.size()) >= k; }

  void offer(const VecHit& hit) {
    if (full()) {
      if (!ranks_before(hit, items[worst_at])) return;
      items[worst_at] = hit;
      recompute_worst();
      return;
    }
    items.push_back(hit);
    if (full()) recompute_worst();
  }

  void recompute_worst() {
    size_t at = 0;
    for (size_t i = 1; i < items.size(); ++i) {
      if (ranks_before(items[at], items[i])) at = i;
    }
    worst_at = at;
  }
};

void scan_range(const float* base, const float* q, const int64_t dims, const int64_t begin,
                const int64_t end, const int64_t k, const int64_t* pks, KeepRowFn keep,
                const void* keep_ctx, std::vector<VecHit>* out) {
  out->clear();
  if (begin >= end || k <= 0) return;
  TopK top(k);
  top.items.reserve(static_cast<size_t>(std::min(k, end - begin)));
  float* const dists = block_scratch(static_cast<size_t>(kBlockRows * dims));
  static thread_local std::vector<int32_t> survivors;
  survivors.resize(static_cast<size_t>(kBlockRows));
  // Running k-th distance. A row at or above it cannot enter the TopK unless it ties, so the filter keeps
  // ties in and drops the rest with one compare instead of a heap visit.
  float threshold = std::numeric_limits<float>::infinity();
  static const bool qtime = ::getenv("MVDB_QTIME") != nullptr;
  double kernel_us = 0.0;
  double filter_us = 0.0;
  int64_t visits = 0;
  const auto qtime_start = std::chrono::steady_clock::now();
  for (int64_t start = begin; start < end; start += kBlockRows) {
    const int64_t count = std::min(kBlockRows, end - start);
    const auto kernel_start = std::chrono::steady_clock::now();
    l2_batch_sq(base + start * dims, q, count, dims, dists);
    if (qtime) {
      kernel_us += std::chrono::duration<double, std::micro>(
                       std::chrono::steady_clock::now() - kernel_start).count();
    }
    if (!top.full()) {
      for (int64_t i = 0; i < count; ++i) {
        const int64_t row = start + i;
        if (keep != nullptr && !keep(keep_ctx, row)) continue;
        VecHit hit;
        hit.dist = dists[i];
        hit.pk = pks[row];
        hit.idx = row;
        top.offer(hit);
      }
      if (top.full()) threshold = static_cast<float>(top.items[top.worst_at].dist);
      continue;
    }
    const auto filter_start = std::chrono::steady_clock::now();
    const int64_t kept = filter_le(dists, count, threshold, survivors.data());
    if (qtime) {
      filter_us += std::chrono::duration<double, std::micro>(
                       std::chrono::steady_clock::now() - filter_start).count();
      visits += kept;
    }
    for (int64_t s = 0; s < kept; ++s) {
      const int64_t i = survivors[static_cast<size_t>(s)];
      const int64_t row = start + i;
      if (keep != nullptr && !keep(keep_ctx, row)) continue;
      VecHit hit;
      hit.dist = dists[i];
      hit.pk = pks[row];
      hit.idx = row;
      top.offer(hit);
    }
    threshold = static_cast<float>(top.items[top.worst_at].dist);
  }
  if (qtime) {
    const double total = std::chrono::duration<double, std::micro>(
                             std::chrono::steady_clock::now() - qtime_start).count();
    std::fprintf(stderr, "[scan] rows=%lld kernel=%.1fus filter=%.1fus visits=%lld total=%.1fus\n",
                 static_cast<long long>(end - begin), kernel_us, filter_us,
                 static_cast<long long>(visits), total);
  }
  out->swap(top.items);
  std::sort(out->begin(), out->end(), ranks_before);
  if (static_cast<int64_t>(out->size()) > k) out->resize(static_cast<size_t>(k));
}

// The scan keeps squared distances; only the surviving rows need the square root.
void take_square_roots(std::vector<VecHit>* hits) {
  for (VecHit& hit : *hits) hit.dist = std::sqrt(hit.dist);
}

class Pool {
 public:
  static Pool& instance() {
    // Deliberately leaked: the worker threads outlive every ordinary object, so there is nothing to
    // tear down and no static-destruction order to worry about.
    static Pool* pool = new Pool();
    return *pool;
  }

  int size() const { return workers_; }

  // Runs `body(worker_index)` on every worker and returns when all of them are done.
  void run(const std::function<void(int)>* body) {
    if (workers_ <= 1) {
      (*body)(0);
      return;
    }
    pthread_mutex_lock(&mutex_);
    body_ = body;
    pending_ = workers_ - 1;
    ++generation_;
    pthread_mutex_unlock(&mutex_);
    pthread_cond_broadcast(&start_);
    (*body)(0);
    pthread_mutex_lock(&mutex_);
    while (pending_ > 0) pthread_cond_wait(&done_, &mutex_);
    body_ = nullptr;
    pthread_mutex_unlock(&mutex_);
  }

 private:
  struct WorkerArg {
    int index = 0;
  };

  Pool() {
    pthread_mutex_init(&mutex_, nullptr);
    pthread_cond_init(&start_, nullptr);
    pthread_cond_init(&done_, nullptr);
    int target = usable_cpu_count();
    if (target > kMaxWorkers) target = kMaxWorkers;
    // For measurement, and for environments where extra threads are counter-productive, the pool size
    // can be pinned; the default is one worker per online CPU, capped above.
    if (const char* pinned = ::getenv("MVDB_SCAN_WORKERS")) {
      const int want = std::atoi(pinned);
      if (want >= 1 && want <= kMaxWorkers) target = want;
    }
    workers_ = 1;   // worker 0 is the calling thread
    for (int i = 1; i < target; ++i) {
      args_[workers_].index = workers_;
      if (pthread_create(&threads_[workers_], nullptr, &Pool::entry, &args_[workers_]) != 0) break;
      ++workers_;
    }
  }

  static void* entry(void* arg) {
    const int index = static_cast<WorkerArg*>(arg)->index;
    instance().worker_loop(index);
    return nullptr;
  }

  void worker_loop(const int index) {
    uint64_t seen = 0;
    for (;;) {
      const std::function<void(int)>* body = nullptr;
      pthread_mutex_lock(&mutex_);
      while (generation_ == seen) pthread_cond_wait(&start_, &mutex_);
      seen = generation_;
      body = body_;
      pthread_mutex_unlock(&mutex_);
      if (body != nullptr) (*body)(index);
      pthread_mutex_lock(&mutex_);
      if (--pending_ == 0) pthread_cond_signal(&done_);
      pthread_mutex_unlock(&mutex_);
    }
  }

  int workers_ = 1;
  pthread_t threads_[kMaxWorkers];
  WorkerArg args_[kMaxWorkers];
  pthread_mutex_t mutex_{};
  pthread_cond_t start_{};
  pthread_cond_t done_{};
  uint64_t generation_ = 0;
  int pending_ = 0;
  const std::function<void(int)>* body_ = nullptr;
};

// Partial answers, reused across queries on the calling thread.
std::vector<std::vector<VecHit>>& partial_scratch(const int workers) {
  static thread_local std::vector<std::vector<VecHit>> partial;
  if (static_cast<int>(partial.size()) != workers) partial.resize(static_cast<size_t>(workers));
  return partial;
}

}  // namespace

void l2_topk_parallel_i8(const int8_t* base, const int8_t* q_codes, int64_t n, int64_t dims,
                         int64_t k, const int64_t* pks, std::vector<VecHit>* out) {
  out->clear();
  if (n <= 0 || dims <= 0 || k <= 0 || base == nullptr || q_codes == nullptr) return;
  const int workers = Pool::instance().size();
  std::vector<std::vector<VecHit>>& partial = partial_scratch(workers);
  const int64_t chunk = (n + workers - 1) / workers;
  auto scan_chunk = [&](const int worker) {
    const int64_t begin = static_cast<int64_t>(worker) * chunk;
    const int64_t end = std::min(n, begin + chunk);
    std::vector<VecHit>& mine = partial[static_cast<size_t>(worker)];
    mine.clear();
    if (begin >= end) return;
    std::vector<std::pair<int32_t, int64_t>> heap;
    heap.reserve(static_cast<size_t>(k) + 1);
    std::vector<int32_t> scratch(static_cast<size_t>(kBlockRows));
    // Same threshold filter as the float scan: once the heap is full, only rows at or below the current
    // k-th value are visited, which is a single integer compare per row instead of a heap update.
    int64_t limit = std::numeric_limits<int64_t>::max();
    for (int64_t start = begin; start < end; start += kBlockRows) {
      const int64_t count = std::min<int64_t>(kBlockRows, end - start);
      l2_batch_i8(base + start * dims, q_codes, count, dims, scratch.data());
      for (int64_t i = 0; i < count; ++i) {
        const int32_t d = scratch[static_cast<size_t>(i)];
        if (d > limit) continue;
        if (static_cast<int64_t>(heap.size()) == k) {
          if (d == heap.front().first && start + i >= heap.front().second) continue;
          std::pop_heap(heap.begin(), heap.end());
          heap.pop_back();
        }
        heap.emplace_back(d, start + i);
        std::push_heap(heap.begin(), heap.end());
      }
      if (static_cast<int64_t>(heap.size()) == k) limit = heap.front().first;
    }
    std::sort(heap.begin(), heap.end());
    mine.reserve(heap.size());
    for (const auto& entry : heap) {
      VecHit hit;
      hit.dist = static_cast<double>(entry.first);
      hit.pk = (pks != nullptr) ? pks[entry.second] : 0;
      hit.idx = entry.second;
      mine.push_back(hit);
    }
  };
  std::function<void(int)> body = [&](const int worker) { scan_chunk(worker); };
  Pool::instance().run(&body);
  size_t total = 0;
  for (const auto& part : partial) total += part.size();
  out->reserve(total);
  for (const auto& part : partial) out->insert(out->end(), part.begin(), part.end());
  std::sort(out->begin(), out->end(), ranks_before);
  if (static_cast<int64_t>(out->size()) > k) out->resize(static_cast<size_t>(k));
}

int scan_worker_count() { return Pool::instance().size(); }

void l2_topk_parallel(const float* base, const float* q, int64_t n, int64_t dims, int64_t k,
                      const int64_t* pks, KeepRowFn keep, const void* keep_ctx,
                      std::vector<VecHit>* out) {
  out->clear();
  if (n <= 0 || dims <= 0 || k <= 0 || base == nullptr || q == nullptr || pks == nullptr) return;

  const int workers = Pool::instance().size();
  if (workers <= 1 || n * dims < kParallelThreshold) {
    scan_range(base, q, dims, 0, n, k, pks, keep, keep_ctx, out);
    take_square_roots(out);
    return;
  }

  std::vector<std::vector<VecHit>>& partial = partial_scratch(workers);
  const int64_t chunk = (n + workers - 1) / workers;
  std::function<void(int)> body = [&](const int worker) {
    const int64_t begin = static_cast<int64_t>(worker) * chunk;
    const int64_t end = std::min(n, begin + chunk);
    scan_range(base, q, dims, begin, end, k, pks, keep, keep_ctx,
               &partial[static_cast<size_t>(worker)]);
  };
  Pool::instance().run(&body);

  size_t total = 0;
  for (const auto& part : partial) total += part.size();
  out->reserve(total);
  for (const auto& part : partial) out->insert(out->end(), part.begin(), part.end());
  std::sort(out->begin(), out->end(), ranks_before);
  if (static_cast<int64_t>(out->size()) > k) out->resize(static_cast<size_t>(k));
  take_square_roots(out);
}

}  // namespace mv
