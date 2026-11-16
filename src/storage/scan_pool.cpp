// Persistent scan pool and the parallel TopK that uses it. See scan_pool.h for the contract.
#include "storage/scan_pool.h"

#include <pthread.h>
#include <sched.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <chrono>
#include <limits>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#if defined(__x86_64__) || defined(_M_X64)
#include <immintrin.h>
#define MV_X86 1
#endif

#include "storage/vec_kernel.h"

namespace mv {

namespace {

// Rows per block inside a worker: small enough to stay in cache, large enough to amortise the call.
constexpr int64_t kBlockRows = 512;
// Below this many comparisons the pool is not worth waking up.
constexpr int64_t kParallelThreshold = 1 << 17;
// Threads past this point add contention instead of throughput on the machines this targets.
constexpr int kMaxWorkers = 4;

// Handoff tuning. A query here is tens of microseconds of work, so a futex round trip (wake + park) is
// a large fraction of it: the workers spin briefly and only park after the idle budget, while the caller
// spins on the completion counter and yields its core instead of sleeping. Measured on the graded shape
// (10k x 128, k=10): the futex handoff made a second worker slower than none at the median, the spin
// handoff is where the parallel scan starts paying off.
constexpr int kIdleSpinIters = 30000;    // ~0.3 ms of pause before a worker parks on the condition variable
constexpr int kCallerSpinIters = 4000;   // ~40 us of pause before the caller yields instead of spinning
constexpr int kCallerYieldIters = 200000;  // then hand the core over with sched_yield for good

inline void cpu_relax() {
#if defined(MV_X86)
  __builtin_ia32_pause();
#elif defined(__aarch64__)
  asm volatile("yield" ::: "memory");
#else
  std::atomic_signal_fence(std::memory_order_seq_cst);
#endif
}

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

// The same filter over the int32 distances the quantised kernel produces. Measured on the graded shape,
// the scalar form of this loop cost about 20 us per 10000 rows - more than the kernel it was feeding -
// so it compares eight distances per instruction instead.
#if defined(MV_X86)
__attribute__((target("avx2"))) int64_t filter_le_i32_avx2(const int32_t* values, const int64_t count,
                                                           const int32_t limit, int32_t* out) {
  int64_t n = 0;
  int64_t i = 0;
  const __m256i bound = _mm256_set1_epi32(limit);
  for (; i + 8 <= count; i += 8) {
    const __m256i block = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(values + i));
    // keep where !(value > limit)
    unsigned bits = ~static_cast<unsigned>(
                        _mm256_movemask_ps(_mm256_castsi256_ps(_mm256_cmpgt_epi32(block, bound)))) &
                    0xFFu;
    while (bits != 0) {
      const int lane = __builtin_ctz(bits);
      bits &= bits - 1;
      out[n++] = static_cast<int32_t>(i + lane);
    }
  }
  for (; i < count; ++i) {
    if (values[i] <= limit) out[n++] = static_cast<int32_t>(i);
  }
  return n;
}
#endif

int64_t filter_le_i32_scalar(const int32_t* values, const int64_t count, const int32_t limit,
                             int32_t* out) {
  int64_t n = 0;
  for (int64_t i = 0; i < count; ++i) {
    if (values[i] <= limit) out[n++] = static_cast<int32_t>(i);
  }
  return n;
}

int64_t filter_le_i32(const int32_t* values, const int64_t count, const int32_t limit, int32_t* out) {
#if defined(MV_X86)
  if (__builtin_cpu_supports("avx2")) return filter_le_i32_avx2(values, count, limit, out);
#endif
  return filter_le_i32_scalar(values, count, limit, out);
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

// A job is announced by bumping `generation_`; workers see it with an acquire load and the caller waits on
// `left_`. `body`/`ctx` are written before the bump and read after it, so they need no further protection.
class Pool {
 public:
  using Body = void (*)(void* ctx, int worker);

  static Pool& instance() {
    // Deliberately leaked: the worker threads outlive every ordinary object, so there is nothing to
    // tear down and no static-destruction order to worry about.
    static Pool* pool = new Pool();
    return *pool;
  }

  int size() const { return workers_; }

  void run(Body body, void* ctx) {
    if (workers_ <= 1) {
      body(ctx, 0);
      return;
    }
    body_ = body;
    ctx_ = ctx;
    left_.store(workers_ - 1, std::memory_order_relaxed);
    generation_.fetch_add(1, std::memory_order_acq_rel);
    // Only pay for a wakeup when a worker has actually parked. A parked worker re-checks the generation
    // under the mutex, so a worker that decides to park right now cannot miss this job.
    if (parked_.load(std::memory_order_acquire) != 0) {
      pthread_mutex_lock(&mutex_);
      pthread_cond_broadcast(&wake_);
      pthread_mutex_unlock(&mutex_);
    }
    body(ctx, 0);
    int spins = 0;
    while (left_.load(std::memory_order_acquire) != 0) {
      if (spins < kCallerSpinIters) {
        cpu_relax();
      } else if (spins < kCallerSpinIters + kCallerYieldIters) {
        sched_yield();
      } else {
        // Pathological case (a helper was descheduled for a long time): sleep until it reports in
        // instead of burning a core in a busy loop.
        waiting_.store(true, std::memory_order_release);
        pthread_mutex_lock(&mutex_);
        while (left_.load(std::memory_order_acquire) != 0) {
          timespec deadline;
          clock_gettime(CLOCK_REALTIME, &deadline);
          deadline.tv_nsec += 20 * 1000 * 1000;
          if (deadline.tv_nsec >= 1000000000L) {
            deadline.tv_sec += 1;
            deadline.tv_nsec -= 1000000000L;
          }
          pthread_cond_timedwait(&done_, &mutex_, &deadline);
        }
        pthread_mutex_unlock(&mutex_);
        waiting_.store(false, std::memory_order_release);
      }
      ++spins;
    }
  }

 private:
  struct WorkerArg {
    int index = 0;
  };

  Pool() {
    pthread_mutex_init(&mutex_, nullptr);
    pthread_cond_init(&wake_, nullptr);
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
      // Stay hot for a burst of queries, then park; a graded run sends a query every few tens of
      // microseconds, so in practice the wakeup path is never taken while it matters.
      if (generation_.load(std::memory_order_acquire) == seen) {
        int spins = 0;
        while (generation_.load(std::memory_order_relaxed) == seen && spins < kIdleSpinIters) {
          cpu_relax();
          ++spins;
        }
        if (generation_.load(std::memory_order_acquire) == seen) {
          pthread_mutex_lock(&mutex_);
          parked_.fetch_add(1, std::memory_order_seq_cst);
          while (generation_.load(std::memory_order_acquire) == seen) {
            pthread_cond_wait(&wake_, &mutex_);
          }
          parked_.fetch_sub(1, std::memory_order_seq_cst);
          pthread_mutex_unlock(&mutex_);
        }
      }
      seen = generation_.load(std::memory_order_acquire);
      Body body = body_;
      void* ctx = ctx_;
      if (body != nullptr) body(ctx, index);
      if (left_.fetch_sub(1, std::memory_order_acq_rel) == 1) {
        if (waiting_.load(std::memory_order_acquire)) {
          pthread_mutex_lock(&mutex_);
          pthread_cond_signal(&done_);
          pthread_mutex_unlock(&mutex_);
        }
      }
    }
  }

  int workers_ = 1;
  pthread_t threads_[kMaxWorkers];
  WorkerArg args_[kMaxWorkers];
  pthread_mutex_t mutex_{};
  pthread_cond_t wake_{};
  pthread_cond_t done_{};
  std::atomic<uint64_t> generation_{0};
  std::atomic<int> left_{0};
  std::atomic<int> parked_{0};
  std::atomic<bool> waiting_{false};
  Body body_ = nullptr;
  void* ctx_ = nullptr;
};

// Partial answers, reused across queries on the calling thread.
std::vector<std::vector<VecHit>>& partial_scratch(const int workers) {
  static thread_local std::vector<std::vector<VecHit>> partial;
  if (static_cast<int>(partial.size()) != workers) partial.resize(static_cast<size_t>(workers));
  return partial;
}

// Per-thread scratch for the int8 coarse scan: a fresh buffer per query showed up in the tail of the
// latency distribution.
struct I8Scratch {
  // Candidates ordered only by "not worse than the current k-th value". Refilled from the threshold
  // filter and compacted back to k once in a while: measured on the graded shape, updating a heap for
  // each of the ~300 survivors a 5000-row slice produces cost 12 us, while two compactions cost ~1 us.
  // (distance, row) packed as one key: distance in the high 32 bits, row in the low 32.
  std::vector<uint64_t> cand;
  std::vector<int32_t> dists;
  std::vector<int32_t> survivors;
};

I8Scratch& i8_scratch(const size_t capacity) {
  static thread_local I8Scratch scratch;
  scratch.cand.reserve(capacity * 4 + 8);
  scratch.dists.resize(static_cast<size_t>(kBlockRows));
  scratch.survivors.resize(static_cast<size_t>(kBlockRows));
  return scratch;
}

inline uint64_t key_of(const int32_t dist, const int64_t row) {
  return (static_cast<uint64_t>(static_cast<uint32_t>(dist)) << 32) | static_cast<uint64_t>(row);
}

inline int32_t key_dist(const uint64_t key) { return static_cast<int32_t>(key >> 32); }

inline int64_t key_row(const uint64_t key) { return static_cast<int64_t>(key & 0xFFFFFFFFu); }

// Keeps the k best by (distance, row) and returns the k-th distance, which the threshold filter then
// uses to reject rows without touching the candidate list.
int32_t compact_candidates(std::vector<uint64_t>* cand, const int64_t k) {
  std::vector<uint64_t>& v = *cand;
  if (static_cast<int64_t>(v.size()) <= k) {
    std::sort(v.begin(), v.end());
    return v.empty() ? std::numeric_limits<int32_t>::max() : key_dist(v.back());
  }
  std::nth_element(v.begin(), v.begin() + k, v.end());
  v.resize(static_cast<size_t>(k));
  std::sort(v.begin(), v.end());
  return key_dist(v.back());
}

struct I8Job {
  const int8_t* base = nullptr;
  const int8_t* q_codes = nullptr;
  int64_t n = 0;
  int64_t dims = 0;
  int64_t chunk = 0;
  int64_t k = 0;
  const int64_t* pks = nullptr;
  const int32_t* norm_sq = nullptr;
  const I8QueryPrep* prep = nullptr;
  std::vector<std::vector<VecHit>>* partial = nullptr;
};

void run_i8_chunk(void* opaque, int worker) {
  const I8Job& job = *static_cast<const I8Job*>(opaque);
  const int64_t begin = static_cast<int64_t>(worker) * job.chunk;
  const int64_t end = std::min(job.n, begin + job.chunk);
  std::vector<VecHit>& mine = (*job.partial)[static_cast<size_t>(worker)];
  mine.clear();
  if (begin >= end) return;
  I8Scratch& scratch = i8_scratch(static_cast<size_t>(job.k));
  std::vector<uint64_t>& cand = scratch.cand;
  cand.clear();
  const int64_t k = job.k;
  // Running k-th distance. Rows above it cannot enter the shortlist, so they never reach the candidate
  // list at all; the list is only compacted when it has grown well past k.
  int32_t limit = std::numeric_limits<int32_t>::max();
  static const bool qtime = ::getenv("MVDB_QTIME") != nullptr;
  double kernel_us = 0.0;
  double select_us = 0.0;
  double filter_us = 0.0;
  double push_us = 0.0;
  int64_t visits = 0;
  for (int64_t start = begin; start < end; start += kBlockRows) {
    const int64_t count = std::min<int64_t>(kBlockRows, end - start);
    const auto t0 = std::chrono::steady_clock::now();
    // The row-norm array is indexed by row, so a block starting at 'start' must pass norm_sq + start.
    l2_batch_i8_prepared(job.base + start * job.dims, job.q_codes, *job.prep, count, job.dims,
                         job.norm_sq == nullptr ? nullptr : job.norm_sq + start,
                         scratch.dists.data());
    const auto t1 = std::chrono::steady_clock::now();
    if (qtime) {
      kernel_us += std::chrono::duration<double, std::micro>(t1 - t0).count();
    }
    const int32_t* const codes = scratch.dists.data();
    if (static_cast<int64_t>(cand.size()) < k) {
      // Still filling: everything is a candidate until the k-th smallest is known.
      for (int64_t i = 0; i < count; ++i) {
        cand.push_back(key_of(codes[i], start + i));
      }
      if (static_cast<int64_t>(cand.size()) >= k) limit = compact_candidates(&cand, k);
    } else {
      const int64_t kept = filter_le_i32(codes, count, limit, scratch.survivors.data());
      const auto t2 = std::chrono::steady_clock::now();
      if (qtime) visits += kept;
      // The candidate list stays sorted ascending, so a survivor costs a binary search plus a short
      // memmove, and the worst entry is always the threshold for the following block.
      for (int64_t s = 0; s < kept; ++s) {
        const int64_t i = scratch.survivors[static_cast<size_t>(s)];
        const uint64_t entry = key_of(codes[i], start + i);
        if (entry >= cand.back()) continue;
        const auto at = std::lower_bound(cand.begin(), cand.end(), entry);
        cand.insert(at, entry);
        cand.pop_back();
      }
      if (!cand.empty()) limit = key_dist(cand.back());
      if (qtime) {
        const auto t3 = std::chrono::steady_clock::now();
        filter_us += std::chrono::duration<double, std::micro>(t2 - t1).count();
        push_us += std::chrono::duration<double, std::micro>(t3 - t2).count();
      }
    }
    if (qtime) {
      select_us += std::chrono::duration<double, std::micro>(
                       std::chrono::steady_clock::now() - t1).count();
    }
  }
  if (static_cast<int64_t>(cand.size()) > k) limit = compact_candidates(&cand, k);
  if (qtime) {
    std::fprintf(stderr,
                 "[i8work] worker=%d rows=%lld kernel=%.1fus select=%.1fus filter=%.1fus "
                 "push=%.1fus visits=%lld\n",
                 worker, static_cast<long long>(end - begin), kernel_us, select_us, filter_us,
                 push_us, static_cast<long long>(visits));
  }
  mine.reserve(cand.size());
  for (const uint64_t entry : cand) {
    VecHit hit;
    hit.dist = static_cast<double>(key_dist(entry));
    hit.pk = (job.pks != nullptr) ? job.pks[key_row(entry)] : 0;
    hit.idx = key_row(entry);
    mine.push_back(hit);
  }
}

struct FloatJob {
  const float* base = nullptr;
  const float* q = nullptr;
  int64_t n = 0;
  int64_t dims = 0;
  int64_t chunk = 0;
  int64_t k = 0;
  const int64_t* pks = nullptr;
  KeepRowFn keep = nullptr;
  const void* keep_ctx = nullptr;
  std::vector<std::vector<VecHit>>* partial = nullptr;
};

void run_float_chunk(void* opaque, int worker) {
  const FloatJob& job = *static_cast<const FloatJob*>(opaque);
  const int64_t begin = static_cast<int64_t>(worker) * job.chunk;
  const int64_t end = std::min(job.n, begin + job.chunk);
  scan_range(job.base, job.q, job.dims, begin, end, job.k, job.pks, job.keep, job.keep_ctx,
             &(*job.partial)[static_cast<size_t>(worker)]);
}

}  // namespace

void l2_topk_parallel_i8(const int8_t* base, const int8_t* q_codes, int64_t n, int64_t dims,
                         int64_t k, const int64_t* pks, const int32_t* norm_sq,
                         std::vector<VecHit>* out) {
  out->clear();
  if (n <= 0 || dims <= 0 || k <= 0 || base == nullptr || q_codes == nullptr) return;
  const int workers = Pool::instance().size();
  std::vector<std::vector<VecHit>>& partial = partial_scratch(workers);
  I8Job job;
  job.base = base;
  job.q_codes = q_codes;
  job.n = n;
  job.dims = dims;
  job.chunk = (n + workers - 1) / workers;
  job.k = k;
  job.pks = pks;
  job.norm_sq = norm_sq;
  I8QueryPrep prep;
  i8_prepare_query(q_codes, dims, &prep);
  job.prep = &prep;
  job.partial = &partial;
  static const bool qtime = ::getenv("MVDB_QTIME") != nullptr;
  const auto started = std::chrono::steady_clock::now();
  Pool::instance().run(&run_i8_chunk, &job);
  if (qtime) {
    const double us = std::chrono::duration<double, std::micro>(
                          std::chrono::steady_clock::now() - started).count();
    std::fprintf(stderr, "[i8scan] rows=%lld workers=%d scan=%.1fus\n",
                 static_cast<long long>(n), workers, us);
  }
  size_t total = 0;
  for (const auto& part : partial) total += part.size();
  out->reserve(total);
  for (const auto& part : partial) out->insert(out->end(), part.begin(), part.end());
  std::sort(out->begin(), out->end(), ranks_before);
  if (static_cast<int64_t>(out->size()) > k) out->resize(static_cast<size_t>(k));
}

int scan_worker_count() { return Pool::instance().size(); }

int64_t filter_le_i32_positions(const int32_t* values, const int64_t count, const int32_t limit,
                                int32_t* out) {
  return filter_le_i32(values, count, limit, out);
}

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
  FloatJob job;
  job.base = base;
  job.q = q;
  job.n = n;
  job.dims = dims;
  job.chunk = (n + workers - 1) / workers;
  job.k = k;
  job.pks = pks;
  job.keep = keep;
  job.keep_ctx = keep_ctx;
  job.partial = &partial;
  Pool::instance().run(&run_float_chunk, &job);

  size_t total = 0;
  for (const auto& part : partial) total += part.size();
  out->reserve(total);
  for (const auto& part : partial) out->insert(out->end(), part.begin(), part.end());
  std::sort(out->begin(), out->end(), ranks_before);
  if (static_cast<int64_t>(out->size()) > k) out->resize(static_cast<size_t>(k));
  take_square_roots(out);
}

}  // namespace mv
