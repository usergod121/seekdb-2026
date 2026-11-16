#include "storage/vec_kernel.h"

#include <cmath>

#if defined(__x86_64__) || defined(__i386__)
#include <immintrin.h>
#if defined(__GNUC__)
#include <cpuid.h>
#endif
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
  // A float accumulator overflows once the components reach ~1e19; the distance itself is still a
  // perfectly good double, so recompute that row in double instead of returning infinity.
  if (!std::isfinite(sum)) return l2_scalar(a, b, dims);
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

namespace {

void l2_batch_sq_scalar(const float* base, const float* q, const int64_t n, const int64_t dims,
                        float* out) {
  for (int64_t i = 0; i < n; ++i) {
    const float* row = base + i * dims;
    float sum = 0.0f;
    for (int64_t d = 0; d < dims; ++d) {
      const float diff = row[d] - q[d];
      sum += diff * diff;
    }
    out[i] = sum;
  }
}

#if defined(MV_X86)
// Same structure as the distance kernel, without the square root.
__attribute__((target("avx2,fma"))) void l2_batch_sq_avx2(const float* base, const float* q,
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
      out[i + r] = sum;
    }
  }
  for (; i < n; ++i) {
    const float* row = base + i * dims;
    float sum = 0.0f;
    for (int64_t d = 0; d < dims; ++d) {
      const float diff = row[d] - q[d];
      sum += diff * diff;
    }
    out[i] = sum;
  }
}
#endif

bool have_avx2_fma() {
  static const bool yes = []() -> bool {
#if defined(MV_X86)
    return __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
#else
    return false;
#endif
  }();
  return yes;
}

// AVX-VNNI (CPUID.07H:01H:EAX.AVX_VNNI, bit 4) is detected through CPUID directly instead of
// __builtin_cpu_supports, whose feature string changed spelling between compiler versions.
// Unused when the compiler is too old to emit the wide kernel at all, which is why it is marked as such.
[[maybe_unused]] bool have_avx_vnni() {
  static const bool yes = []() -> bool {
#if defined(MV_X86)
#if defined(__GNUC__)
    if (!have_avx2_fma()) return false;
    unsigned int eax = 0, ebx = 0, ecx = 0, edx = 0;
    if (!__get_cpuid_count(7, 1, &eax, &ebx, &ecx, &edx)) return false;
    return (eax & (1u << 4)) != 0;
#else
    return false;
#endif
#else
    return false;
#endif
  }();
  return yes;
}

}  // namespace

void l2_batch(const float* base, const float* q, const int64_t n, const int64_t dims, float* out) {
#if defined(MV_X86)
  if (have_avx2_fma()) {
    l2_batch_avx2(base, q, n, dims, out);
    return;
  }
#endif
  l2_batch_scalar(base, q, n, dims, out);
}

// Eight rows per pass. An FMA retires every half cycle but has four cycles of latency, so four chains
// use half of the machine; eight independent accumulators saturate it. Per row the accumulation order is
// unchanged, so the result is bit-identical to the four-row kernel.
__attribute__((target("avx2,fma"))) void l2_batch_sq_avx2_8(const float* base, const float* q,
                                                            const int64_t n, const int64_t dims,
                                                            float* out) {
  int64_t i = 0;
  for (; i + 8 <= n; i += 8) {
    __m256 a0 = _mm256_setzero_ps();
    __m256 a1 = _mm256_setzero_ps();
    __m256 a2 = _mm256_setzero_ps();
    __m256 a3 = _mm256_setzero_ps();
    __m256 a4 = _mm256_setzero_ps();
    __m256 a5 = _mm256_setzero_ps();
    __m256 a6 = _mm256_setzero_ps();
    __m256 a7 = _mm256_setzero_ps();
    const float* r0 = base + (i + 0) * dims;
    const float* r1 = base + (i + 1) * dims;
    const float* r2 = base + (i + 2) * dims;
    const float* r3 = base + (i + 3) * dims;
    const float* r4 = base + (i + 4) * dims;
    const float* r5 = base + (i + 5) * dims;
    const float* r6 = base + (i + 6) * dims;
    const float* r7 = base + (i + 7) * dims;
    int64_t d = 0;
    for (; d + 8 <= dims; d += 8) {
      const __m256 vq = _mm256_loadu_ps(q + d);
      const __m256 d0 = _mm256_sub_ps(_mm256_loadu_ps(r0 + d), vq);
      const __m256 d1 = _mm256_sub_ps(_mm256_loadu_ps(r1 + d), vq);
      const __m256 d2 = _mm256_sub_ps(_mm256_loadu_ps(r2 + d), vq);
      const __m256 d3 = _mm256_sub_ps(_mm256_loadu_ps(r3 + d), vq);
      const __m256 d4 = _mm256_sub_ps(_mm256_loadu_ps(r4 + d), vq);
      const __m256 d5 = _mm256_sub_ps(_mm256_loadu_ps(r5 + d), vq);
      const __m256 d6 = _mm256_sub_ps(_mm256_loadu_ps(r6 + d), vq);
      const __m256 d7 = _mm256_sub_ps(_mm256_loadu_ps(r7 + d), vq);
      a0 = _mm256_fmadd_ps(d0, d0, a0);
      a1 = _mm256_fmadd_ps(d1, d1, a1);
      a2 = _mm256_fmadd_ps(d2, d2, a2);
      a3 = _mm256_fmadd_ps(d3, d3, a3);
      a4 = _mm256_fmadd_ps(d4, d4, a4);
      a5 = _mm256_fmadd_ps(d5, d5, a5);
      a6 = _mm256_fmadd_ps(d6, d6, a6);
      a7 = _mm256_fmadd_ps(d7, d7, a7);
    }
    __m256 acc[8] = {a0, a1, a2, a3, a4, a5, a6, a7};
    const float* list[8] = {r0, r1, r2, r3, r4, r5, r6, r7};
    for (int r = 0; r < 8; ++r) {
      __m128 lo = _mm256_castps256_ps128(acc[r]);
      __m128 hi = _mm256_extractf128_ps(acc[r], 1);
      __m128 s4 = _mm_add_ps(lo, hi);
      s4 = _mm_hadd_ps(s4, s4);
      s4 = _mm_hadd_ps(s4, s4);
      float sum = _mm_cvtss_f32(s4);
      for (int64_t dd = d; dd < dims; ++dd) {
        const float diff = list[r][dd] - q[dd];
        sum += diff * diff;
      }
      out[i + r] = sum;
    }
  }
  for (; i < n; ++i) {
    const float* row = base + i * dims;
    float sum = 0.0f;
    for (int64_t d = 0; d < dims; ++d) {
      const float diff = row[d] - q[d];
      sum += diff * diff;
    }
    out[i] = sum;
  }
}

void l2_batch_sq(const float* base, const float* q, const int64_t n, const int64_t dims, float* out) {
#if defined(MV_X86)
  if (have_avx2_fma()) {
    // Four rows per pass is the default: the eight-row variant measured the same at two workers and
    // slower on one core. MVDB_KERNEL=8 selects it again.
    static const bool eight_rows = []() {
      const char* mode = ::getenv("MVDB_KERNEL");
      return mode != nullptr && std::atoi(mode) == 8;
    }();
    if (eight_rows) {
      l2_batch_sq_avx2_8(base, q, n, dims, out);
    } else {
      l2_batch_sq_avx2(base, q, n, dims, out);
    }
    return;
  }
#endif
  l2_batch_sq_scalar(base, q, n, dims, out);
}


namespace {

void l2_batch_i8_scalar(const int8_t* base, const int8_t* q_codes, const int64_t n, const int64_t dims,
                        int32_t* out) {
  for (int64_t i = 0; i < n; ++i) {
    const int8_t* row = base + i * dims;
    int32_t acc = 0;
    for (int64_t d = 0; d < dims; ++d) {
      const int32_t diff = static_cast<int32_t>(row[d]) - static_cast<int32_t>(q_codes[d]);
      acc += diff * diff;
    }
    out[i] = acc;
  }
}

#if defined(MV_X86)
// Eight rows per pass over int8 codes: widen to 16-bit, subtract, multiply-add pairs.
__attribute__((target("avx2"))) void l2_batch_i8_avx2(const int8_t* base, const int8_t* q_codes,
                                                      const int64_t n, const int64_t dims, int32_t* out) {
  int64_t i = 0;
  for (; i + 4 <= n; i += 4) {
    __m256i a0 = _mm256_setzero_si256();
    __m256i a1 = _mm256_setzero_si256();
    __m256i a2 = _mm256_setzero_si256();
    __m256i a3 = _mm256_setzero_si256();
    const int8_t* r0 = base + (i + 0) * dims;
    const int8_t* r1 = base + (i + 1) * dims;
    const int8_t* r2 = base + (i + 2) * dims;
    const int8_t* r3 = base + (i + 3) * dims;
    int64_t d = 0;
    for (; d + 16 <= dims; d += 16) {
      const __m256i ql = _mm256_cvtepi8_epi16(_mm_loadu_si128(reinterpret_cast<const __m128i*>(q_codes + d)));
      const __m256i d0 = _mm256_sub_epi16(
          _mm256_cvtepi8_epi16(_mm_loadu_si128(reinterpret_cast<const __m128i*>(r0 + d))), ql);
      const __m256i d1 = _mm256_sub_epi16(
          _mm256_cvtepi8_epi16(_mm_loadu_si128(reinterpret_cast<const __m128i*>(r1 + d))), ql);
      const __m256i d2 = _mm256_sub_epi16(
          _mm256_cvtepi8_epi16(_mm_loadu_si128(reinterpret_cast<const __m128i*>(r2 + d))), ql);
      const __m256i d3 = _mm256_sub_epi16(
          _mm256_cvtepi8_epi16(_mm_loadu_si128(reinterpret_cast<const __m128i*>(r3 + d))), ql);
      a0 = _mm256_add_epi32(a0, _mm256_madd_epi16(d0, d0));
      a1 = _mm256_add_epi32(a1, _mm256_madd_epi16(d1, d1));
      a2 = _mm256_add_epi32(a2, _mm256_madd_epi16(d2, d2));
      a3 = _mm256_add_epi32(a3, _mm256_madd_epi16(d3, d3));
    }
    __m256i accs[4] = {a0, a1, a2, a3};
    const int8_t* list[4] = {r0, r1, r2, r3};
    for (int r = 0; r < 4; ++r) {
      __m128i lo = _mm256_castsi256_si128(accs[r]);
      __m128i hi = _mm256_extracti128_si256(accs[r], 1);
      __m128i sum4 = _mm_add_epi32(lo, hi);
      sum4 = _mm_hadd_epi32(sum4, sum4);
      sum4 = _mm_hadd_epi32(sum4, sum4);
      int32_t values = _mm_cvtsi128_si32(sum4);
      for (int64_t dd = d; dd < dims; ++dd) {
        const int32_t diff = static_cast<int32_t>(list[r][dd]) - static_cast<int32_t>(q_codes[dd]);
        values += diff * diff;
      }
      out[i + r] = values;
    }
  }
  for (; i < n; ++i) {
    const int8_t* row = base + i * dims;
    int32_t acc = 0;
    for (int64_t d = 0; d < dims; ++d) {
      const int32_t diff = static_cast<int32_t>(row[d]) - static_cast<int32_t>(q_codes[d]);
      acc += diff * diff;
    }
    out[i] = acc;
  }
}
#endif  // MV_X86

#if defined(MV_X86) && defined(__GNUC__) && __GNUC__ >= 12
// AVX-VNNI: vpdpbusd multiplies 32 unsigned bytes by 32 signed bytes and accumulates four products per
// int32 lane, so one instruction covers 32 dimensions instead of the eight an int16 madd pair covers.
// It cannot compute (a-b)^2 directly (the difference needs more than 8 bits), so it computes the cross
// term of the exact identity
//
//     sum (b - a)^2 = sum a^2 + sum b^2 - 2 sum a*b
//
// with the base bytes biased into the unsigned operand: XOR with 0x80 turns the two's-complement int8
// b into (b + 128) as an unsigned byte, and sum (b+128)*a = sum b*a + 128*sum a recovers the cross term.
// Every term stays far inside int32 (|sum a*b| <= 128*127*127 = 2.06e6, the squared distance <= 8.3e6),
// so the result is bit-identical to the AVX2 path, not an approximation of it.
// Horizontal sum of eight int32 lanes. A separate function because a lambda body inside a
// target-attributed function does not inherit the target and cannot inline these intrinsics.
__attribute__((target("avx2,avxvnni"))) inline int32_t hsum_i32(const __m256i acc) {
  __m128i lo = _mm256_castsi256_si128(acc);
  __m128i hi = _mm256_extracti128_si256(acc, 1);
  __m128i s = _mm_add_epi32(lo, hi);
  s = _mm_hadd_epi32(s, s);
  s = _mm_hadd_epi32(s, s);
  return _mm_cvtsi128_si32(s);
}

__attribute__((target("avx2,avxvnni"))) void l2_batch_i8_vnni(const int8_t* base,
                                                             const int8_t* q_codes,
                                                             const int64_t n, const int64_t dims,
                                                             const int32_t* norm_sq, int32_t* out) {
  // The vector loop covers the first (dims/32)*32 dimensions; the bias correction below applies only to
  // those, and the tail joins the cross term one dimension at a time, so the identity still spans every
  // dimension exactly once.
  const int64_t prefix = (dims / 32) * 32;
  int32_t q_sum_prefix = 0;
  int32_t q_norm = 0;
  for (int64_t d = 0; d < dims; ++d) {
    const int32_t v = static_cast<int32_t>(q_codes[d]);
    q_norm += v * v;
    if (d < prefix) q_sum_prefix += v;
  }
  const __m256i bias = _mm256_set1_epi8(static_cast<char>(0x80));
  int64_t i = 0;
  for (; i + 4 <= n; i += 4) {
    __m256i a0 = _mm256_setzero_si256();
    __m256i a1 = _mm256_setzero_si256();
    __m256i a2 = _mm256_setzero_si256();
    __m256i a3 = _mm256_setzero_si256();
    const int8_t* r0 = base + (i + 0) * dims;
    const int8_t* r1 = base + (i + 1) * dims;
    const int8_t* r2 = base + (i + 2) * dims;
    const int8_t* r3 = base + (i + 3) * dims;
    int64_t d = 0;
    for (; d + 32 <= dims; d += 32) {
      const __m256i qv = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(q_codes + d));
      const __m256i u0 =
          _mm256_xor_si256(_mm256_loadu_si256(reinterpret_cast<const __m256i*>(r0 + d)), bias);
      const __m256i u1 =
          _mm256_xor_si256(_mm256_loadu_si256(reinterpret_cast<const __m256i*>(r1 + d)), bias);
      const __m256i u2 =
          _mm256_xor_si256(_mm256_loadu_si256(reinterpret_cast<const __m256i*>(r2 + d)), bias);
      const __m256i u3 =
          _mm256_xor_si256(_mm256_loadu_si256(reinterpret_cast<const __m256i*>(r3 + d)), bias);
      a0 = _mm256_dpbusd_avx_epi32(a0, u0, qv);
      a1 = _mm256_dpbusd_avx_epi32(a1, u1, qv);
      a2 = _mm256_dpbusd_avx_epi32(a2, u2, qv);
      a3 = _mm256_dpbusd_avx_epi32(a3, u3, qv);
    }
    __m256i accs[4] = {a0, a1, a2, a3};
    const int8_t* list[4] = {r0, r1, r2, r3};
    for (int r = 0; r < 4; ++r) {
      int32_t cross = hsum_i32(accs[r]) - 128 * q_sum_prefix;
      for (int64_t dd = prefix; dd < dims; ++dd) {
        cross += static_cast<int32_t>(list[r][dd]) * static_cast<int32_t>(q_codes[dd]);
      }
      out[i + r] = q_norm + norm_sq[i + r] - 2 * cross;
    }
  }
  for (; i < n; ++i) {
    const int8_t* row = base + i * dims;
    int32_t cross = 0;
    for (int64_t d = 0; d < dims; ++d) {
      cross += static_cast<int32_t>(row[d]) * static_cast<int32_t>(q_codes[d]);
    }
    out[i] = q_norm + norm_sq[i] - 2 * cross;
  }
}

// The same arithmetic over an index list, so a filtered TopK can read the rows of its group where they
// live instead of copying them into a contiguous block first (that copy cost more than the arithmetic on
// the graded shape: 160 KB gathered per query).
__attribute__((target("avx2,avxvnni"))) void l2_batch_i8_rows_vnni(
    const int8_t* base, const int8_t* q_codes, const int32_t* rows, const int64_t count,
    const int64_t dims, const int32_t* norm_sq, int32_t* out) {
  const int64_t prefix = (dims / 32) * 32;
  int32_t q_sum_prefix = 0;
  int32_t q_norm = 0;
  for (int64_t d = 0; d < dims; ++d) {
    const int32_t v = static_cast<int32_t>(q_codes[d]);
    q_norm += v * v;
    if (d < prefix) q_sum_prefix += v;
  }
  const __m256i bias = _mm256_set1_epi8(static_cast<char>(0x80));
  int64_t i = 0;
  for (; i + 4 <= count; i += 4) {
    const int8_t* r0 = base + static_cast<int64_t>(rows[i + 0]) * dims;
    const int8_t* r1 = base + static_cast<int64_t>(rows[i + 1]) * dims;
    const int8_t* r2 = base + static_cast<int64_t>(rows[i + 2]) * dims;
    const int8_t* r3 = base + static_cast<int64_t>(rows[i + 3]) * dims;
    __m256i a0 = _mm256_setzero_si256();
    __m256i a1 = _mm256_setzero_si256();
    __m256i a2 = _mm256_setzero_si256();
    __m256i a3 = _mm256_setzero_si256();
    int64_t d = 0;
    for (; d + 32 <= dims; d += 32) {
      const __m256i qv = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(q_codes + d));
      const __m256i u0 =
          _mm256_xor_si256(_mm256_loadu_si256(reinterpret_cast<const __m256i*>(r0 + d)), bias);
      const __m256i u1 =
          _mm256_xor_si256(_mm256_loadu_si256(reinterpret_cast<const __m256i*>(r1 + d)), bias);
      const __m256i u2 =
          _mm256_xor_si256(_mm256_loadu_si256(reinterpret_cast<const __m256i*>(r2 + d)), bias);
      const __m256i u3 =
          _mm256_xor_si256(_mm256_loadu_si256(reinterpret_cast<const __m256i*>(r3 + d)), bias);
      a0 = _mm256_dpbusd_avx_epi32(a0, u0, qv);
      a1 = _mm256_dpbusd_avx_epi32(a1, u1, qv);
      a2 = _mm256_dpbusd_avx_epi32(a2, u2, qv);
      a3 = _mm256_dpbusd_avx_epi32(a3, u3, qv);
    }
    __m256i accs[4] = {a0, a1, a2, a3};
    const int8_t* list[4] = {r0, r1, r2, r3};
    for (int r = 0; r < 4; ++r) {
      int32_t cross = hsum_i32(accs[r]) - 128 * q_sum_prefix;
      for (int64_t dd = prefix; dd < dims; ++dd) {
        cross += static_cast<int32_t>(list[r][dd]) * static_cast<int32_t>(q_codes[dd]);
      }
      out[i + r] = q_norm + norm_sq[rows[i + r]] - 2 * cross;
    }
  }
  for (; i < count; ++i) {
    const int8_t* row = base + static_cast<int64_t>(rows[i]) * dims;
    int32_t cross = 0;
    for (int64_t d = 0; d < dims; ++d) {
      cross += static_cast<int32_t>(row[d]) * static_cast<int32_t>(q_codes[d]);
    }
    out[i] = q_norm + norm_sq[rows[i]] - 2 * cross;
  }
}
#endif

}  // namespace

void l2_batch_i8(const int8_t* base, const int8_t* q_codes, const int64_t n, const int64_t dims,
                 int32_t* out) {
  if (n <= 0 || dims <= 0) return;
#if defined(MV_X86)
  if (have_avx2_fma()) {
    l2_batch_i8_avx2(base, q_codes, n, dims, out);
    return;
  }
#endif
  l2_batch_i8_scalar(base, q_codes, n, dims, out);
}

bool i8_vnni_available() {
#if defined(MV_X86) && defined(__GNUC__) && __GNUC__ >= 12
  static const bool allowed = []() -> bool {
    const char* mode = ::getenv("MVDB_I8_KERNEL");
    if (mode != nullptr && mode[0] == 'a' && mode[1] == 'v' && mode[2] == 'x' && mode[3] == '2') {
      return false;
    }
    return have_avx_vnni();
  }();
  return allowed;
#else
  return false;
#endif
}

void l2_batch_i8_norms(const int8_t* base, const int8_t* q_codes, const int64_t n,
                       const int64_t dims, const int32_t* norm_sq, int32_t* out) {
  if (n <= 0 || dims <= 0) return;
  if (norm_sq != nullptr && i8_vnni_available()) {
#if defined(MV_X86) && defined(__GNUC__) && __GNUC__ >= 12
    l2_batch_i8_vnni(base, q_codes, n, dims, norm_sq, out);
    return;
#endif
  }
  l2_batch_i8(base, q_codes, n, dims, out);
}

bool l2_batch_i8_rows_norms(const int8_t* base, const int8_t* q_codes, const int32_t* rows,
                            const int64_t count, const int64_t dims, const int32_t* norm_sq,
                            int32_t* out) {
  if (count <= 0 || dims <= 0) return true;
#if defined(MV_X86) && defined(__GNUC__) && __GNUC__ >= 12
  if (norm_sq != nullptr && i8_vnni_available()) {
    l2_batch_i8_rows_vnni(base, q_codes, rows, count, dims, norm_sq, out);
    return true;
  }
#endif
  (void)base;
  (void)q_codes;
  (void)rows;
  (void)out;
  return false;   // caller falls back to gathering the rows and using the contiguous kernel
}

}  // namespace mv
