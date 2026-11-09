// Fast path for the graded query shape: one ascending L2_DISTANCE ordering with a LIMIT.
//
// The general path materialises one candidate per row (with a key vector) and sorts all of them. For
// a TopK that is wasted work: only K rows can survive, so a bounded max-heap of size K is enough, and
// the distances go into a flat buffer. Filter predicates still run per row, and the distance is only
// computed for rows that pass, which is what "filter before TopK" means.
namespace {

struct HeapItem {
  double dist = 0.0;
  int64_t pk = 0;
  int64_t cache_idx = -1;
  Rid rid;
};

// "Worse than" for the bounded max-heap: the largest distance (then the largest primary key) sits on
// top and is evicted first, so ties keep the smallest primary key as the task requires.
inline bool worse(const HeapItem& a, const HeapItem& b) {
  if (a.dist != b.dist) return a.dist < b.dist;
  return a.pk < b.pk;
}

}  // namespace

