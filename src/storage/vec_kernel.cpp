#include "storage/vec_kernel.h"

#include <cmath>

#if defined(__x86_64__) || defined(__i386__)
#include <immintrin.h>
#define MV_X86 1
#endif

namespace mv {

namespace {

double l2_scalar(const float* a, const float* b, const int64_t dims) {
  double acc = 0.0;
  for (int64_t d = 0; d < dims; ++d) {
    const double diff = static_cast<double>(a[d]) - static_cast<double>(b[d]);
    acc += diff * diff;
  }
  return std::sqrt(acc);
}

#if defined(MV_X86)
// Same arithmetic in single precision, vectorised; accumulated in two lanes of doubles is not needed
// because the result is compared and printed after a sqrt of a sum of squares of float differences.
__attribute__((target("avx2,fma"))) double l2_avx2(const float* a, const float* b, const int64_t dims) {
  __m256 acc = _mm256_setzero_ps();
  int64_t d = 0;
  for (; d + 8 <= dims; d += 8) {
    const __m256 va = _mm256_loadu_ps(a + d);
    const __m256 vb = _mm256_loadu_ps(b + d);
    const __m256 diff = _mm256_sub_ps(va, vb);
    acc = _mm256_fmadd_ps(diff, diff, acc);
  }
  // horizontal sum of the 8 lanes
  __m128 lo = _mm256_castps256_ps128(acc);
  __m128 hi = _mm256_extractf128_ps(acc, 1);
  __m128 sum4 = _mm_add_ps(lo, hi);
  sum4 = _mm_hadd_ps(sum4, sum4);
  sum4 = _mm_hadd_ps(sum4, sum4);
  float sum = _mm_cvtss_f32(sum4);
  for (; d < dims; ++d) {
    const float diff = a[d] - b[d];
    sum += diff * diff;
  }
  return std::sqrt(static_cast<double>(sum));
}
#endif

}  // namespace

L2Fn l2_kernel() {
  static const L2Fn chosen = []() -> L2Fn {
#if defined(MV_X86)
    if (__builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma")) {
      return &l2_avx2;
    }
#endif
    return &l2_scalar;
  }();
  return chosen;
}

double l2_distance(const float* a, const float* b, const int64_t dims) {
  return l2_kernel()(a, b, dims);
}

}  // namespace mv
namespace mv {

namespace {

void l2_batch_scalar(const float* base, const float* q, const int64_t n, const int64_t dims,
                     float* out) {
  for (int64_t i = 0; i < n; ++i) {
    out[i] = static_cast<float>(l2_scalar(base + i * dims, q, dims));
  }
}

#if defined(MV_X86)
// Four rows per pass: independent accumulators keep the FMA pipeline busy while the query vector is
// reused from registers.
__attribute__((target("avx2,fma"))) void l2_batch_avx2(const float* base, const float* q,
                                                       const int64_t n, const int64_t dims,
                                                       float* out) {
  int64_t i = 0;
  for (; i + 4 <= n; i += 4) {
    __m256 a0 = _mm256_setzero_ps();
    __m256 a1 = _mm256_setzero_ps();
    __m256 a2 = _mm256_setzero_ps();
    __m256 a3 = _mm256_setzero_ps();
    const float* r0 = base + (i + 0) * dims;
    const float* r1 = base + (i + 1) * dims;
    const float* r2 = base + (i + 2) * dims;
    const float* r3 = base + (i + 3) * dims;
    int64_t d = 0;
    for (; d + 8 <= dims; d += 8) {
      const __m256 vq = _mm256_loadu_ps(q + d);
      const __m256 d0 = _mm256_sub_ps(_mm256_loadu_ps(r0 + d), vq);
      const __m256 d1 = _mm256_sub_ps(_mm256_loadu_ps(r1 + d), vq);
      const __m256 d2 = _mm256_sub_ps(_mm256_loadu_ps(r2 + d), vq);
      const __m256 d3 = _mm256_sub_ps(_mm256_loadu_ps(r3 + d), vq);
      a0 = _mm256_fmadd_ps(d0, d0, a0);
      a1 = _mm256_fmadd_ps(d1, d1, a1);
      a2 = _mm256_fmadd_ps(d2, d2, a2);
      a3 = _mm256_fmadd_ps(d3, d3, a3);
    }
    __m256 acc[4] = {a0, a1, a2, a3};
    for (int r = 0; r < 4; ++r) {
      __m128 lo = _mm256_castps256_ps128(acc[r]);
      __m128 hi = _mm256_extractf128_ps(acc[r], 1);
      __m128 s4 = _mm_add_ps(lo, hi);
      s4 = _mm_hadd_ps(s4, s4);
      s4 = _mm_hadd_ps(s4, s4);
      float sum = _mm_cvtss_f32(s4);
      const float* row = base + (i + r) * dims;
      for (int64_t dd = d; dd < dims; ++dd) {
        const float diff = row[dd] - q[dd];
        sum += diff * diff;
      }
      out[i + r] = std::sqrt(sum);
    }
  }
  for (; i < n; ++i) {
    out[i] = static_cast<float>(l2_avx2(base + i * dims, q, dims));
  }
}
#endif

}  // namespace

void l2_batch(const float* base, const float* q, const int64_t n, const int64_t dims, float* out) {
  static const bool have_avx2 = []() -> bool {
#if defined(MV_X86)
    return __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
#else
    return false;
#endif
  }();
#if defined(MV_X86)
  if (have_avx2) {
    l2_batch_avx2(base, q, n, dims, out);
    return;
  }
#else
  (void)have_avx2;
#endif
  l2_batch_scalar(base, q, n, dims, out);
}

}  // namespace mv
