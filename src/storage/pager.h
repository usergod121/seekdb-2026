// Page file with a bounded buffer pool.
//
// The data file is an array of fixed size pages. Only a bounded number of pages are resident, so a
// data set larger than the memory budget still works: a page that is needed and not resident is read
// from the file, and the least recently used resident page is written back first when it is dirty.
// The engine is single threaded (one client, one statement at a time), so no latching is needed.
#pragma once

#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

#include "common.h"

namespace mv {

// A row must fit one page (the heap has no overflow chain), so the page is sized to the largest
// row MySQL itself allows - a VARCHAR column may hold 65535 bytes - instead of 8 KiB, which
// rejected perfectly legal wide rows. Slot offsets are u16, so 64 KiB is the ceiling.
constexpr int64_t kPageSize = 65536;

class Pager {
 public:
  Pager() = default;
  ~Pager();

  // Opens (or creates) the page file. `pool_pages` bounds how many pages stay resident.
  Err open(const std::string& path, int64_t pool_pages);
  Err close();

  // Returns a pointer to the page, reading it in if necessary. The pointer stays valid until the next
  // call that can evict (any get/alloc/flush). Callers must not hold it across such calls.
  Err get(int32_t page_id, char** page);
  // Appends a new zeroed page.
  Err alloc(int32_t* page_id, char** page);
  // Marks the resident page dirty so it is written back on eviction/flush.
  void mark_dirty(int32_t page_id);
  // Drops this file's pages from the kernel page cache. Once the table cache serves the scans
  // the cached file pages are pure waste, and peak memory counts the page cache too.
  void drop_page_cache();
  // Returns every clean frame to the free list. Dirty frames stay, because they hold changes that have
  // not reached the file yet. Used once a table's rows live in the in-memory cache and page access is
  // no longer on the hot path.
  void release_clean_frames();
  // Writes every dirty page and fsyncs the file.
  Err flush();
  // Bytes the page file currently occupies (a proxy for the payload the heap holds).
  int64_t page_bytes() const { return page_count_ * kPageSize; }

  int64_t page_count() const { return page_count_; }
  int64_t resident_pages() const { return static_cast<int64_t>(frames_.size()); }
  int64_t pool_capacity() const { return pool_pages_; }

 private:
  struct Frame {
    char* data = nullptr;   // kPageSize bytes
    int32_t page_id = -1;
    bool dirty = false;
    uint64_t last_used = 0;
  };

  Frame* find_or_load(int32_t page_id, Err* err);
  Frame* pick_victim(Err* err);
  Err write_back(Frame* f);

  int fd_ = -1;
  int64_t page_count_ = 0;
  int64_t pool_pages_ = 256;
  uint64_t clock_ = 0;
  std::unordered_map<int32_t, Frame*> frames_;   // page_id -> frame
  std::vector<Frame*> free_frames_;              // preallocated frame objects
};

}  // namespace mv
