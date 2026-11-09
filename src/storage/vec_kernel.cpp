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
