#pragma once
// Minimal stage profiler. Disabled unless MVDB_PROFILE is set in the environment, so the normal build
// pays one predictable branch per measurement. Times are accumulated per stage and printed to stderr
// when the process shuts down.
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>

namespace mv {

enum ProfSlot {
  kProfParse = 0,
  kProfInsertLoop,
  kProfEncodeRow,
  kProfHeapInsert,
  kProfPkIndex,
  kProfUndo,
  kProfWalAppend,
  kProfWalCommit,
  kProfSelectScan,
  kProfJson,
  kProfSplit,
  kProfCopyStmt,
  kProfSlots
};

inline const char* prof_name(const int slot) {
  switch (slot) {
    case kProfParse: return "parse";
    case kProfInsertLoop: return "insert-loop";
    case kProfEncodeRow: return "encode-row";
    case kProfHeapInsert: return "heap-insert";
    case kProfPkIndex: return "pk-index";
    case kProfUndo: return "undo";
    case kProfWalAppend: return "wal-append";
    case kProfWalCommit: return "wal-commit";
    case kProfSelectScan: return "select-scan";
    case kProfJson: return "json-request";
    case kProfSplit: return "split-statements";
    case kProfCopyStmt: return "copy-statement";
    default: return "?";
  }
}

class Prof {
 public:
  static bool on() {
    static const bool enabled = ::getenv("MVDB_PROFILE") != nullptr;
    return enabled;
  }

  static inline uint64_t now() {
    return static_cast<uint64_t>(std::chrono::steady_clock::now().time_since_epoch().count());
  }

  static void add(const int slot, const uint64_t delta) {
    nanos_[slot] += delta;
    ++calls_[slot];
  }

  static void dump() {
    if (!on()) return;
    std::fprintf(stderr, "[profile] stage           total_ms      calls      us/call\n");
    for (int i = 0; i < kProfSlots; ++i) {
      if (calls_[i] == 0) continue;
      const double total = static_cast<double>(nanos_[i]) / 1e6;
      const double per = static_cast<double>(nanos_[i]) / 1e3 / static_cast<double>(calls_[i]);
      std::fprintf(stderr, "[profile] %-14s %10.3f %10llu %10.3f\n", prof_name(i), total,
                   static_cast<unsigned long long>(calls_[i]), per);
    }
    std::fflush(stderr);
  }

 private:
  inline static uint64_t nanos_[kProfSlots] = {0};
  inline static uint64_t calls_[kProfSlots] = {0};
};

// Times the enclosing scope into one slot when profiling is on.
class ProfScope {
 public:
  explicit ProfScope(const int slot) : slot_(slot) {
    if (Prof::on()) {
      active_ = true;
      started_ = Prof::now();
    }
  }
  ~ProfScope() {
    if (active_) Prof::add(slot_, Prof::now() - started_);
  }

 private:
  int slot_;
  bool active_ = false;
  uint64_t started_ = 0;
};

}  // namespace mv
