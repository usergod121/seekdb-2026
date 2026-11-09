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

}  // namespace mv
