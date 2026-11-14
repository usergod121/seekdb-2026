// Distance kernels.
//
// The scan is the hot path of a TopK query, so the L2 distance runs over a contiguous float array and
// uses AVX2 + FMA when the CPU has them. The feature check happens once and picks a function pointer,
// so a build stays portable: on a machine without AVX2 the scalar path runs instead of trapping.
#pragma once

#include <cstdint>

namespace mv {

using L2Fn = double (*)(const float* a, const float* b, int64_t dims);

// Resolved once: the widest implementation the CPU supports.
L2Fn l2_kernel();

// Convenience wrapper around the resolved kernel.
double l2_distance(const float* a, const float* b, int64_t dims);

// Computes n distances from consecutive vectors at `base` (row i at base + i*dims) against `q`.
// Batch form so the contiguous cache is walked with one call per block instead of a call per row.
void l2_batch(const float* base, const float* q, int64_t n, int64_t dims, float* out);

// Same walk, but each entry is the SQUARED distance. A TopK only needs the order, and the square root
// is monotone, so the k surviving rows can take it instead of every row.
void l2_batch_sq(const float* base, const float* q, int64_t n, int64_t dims, float* out);

// Squared distances from an int8 copy (one global scale): used to shortlist rows, which are then
// reranked with exact float distances.
void l2_batch_i8(const int8_t* base, const int8_t* q_codes, int64_t n, int64_t dims, int32_t* out);

}  // namespace mv



