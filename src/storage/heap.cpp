#include "storage/heap.h"

#include <algorithm>
#include <cstring>

namespace mv {

namespace {

constexpr int32_t kHeaderBytes = 8;
constexpr int32_t kSlotBytes = 4;

inline uint16_t load_u16(const char* p) {
  uint16_t v = 0;
  std::memcpy(&v, p, 2);
  return v;
}
inline void store_u16(char* p, const uint16_t v) { std::memcpy(p, &v, 2); }

// Space available for a new payload of `len` bytes plus one slot entry.
int32_t page_free(const char* page, const int32_t len) {
  const uint16_t slots = load_u16(page);
  int32_t used_lo = kHeaderBytes + static_cast<int32_t>(slots) * kSlotBytes;
  int32_t used_hi = kPageSize;
  for (uint16_t i = 0; i < slots; ++i) {
    const int32_t off = load_u16(page + kHeaderBytes + i * kSlotBytes);
    const int32_t l = load_u16(page + kHeaderBytes + i * kSlotBytes + 2);
    if (l != 0 && off < used_hi) used_hi = off;
  }
  const int32_t free_gap = used_hi - used_lo;
  const int32_t need = static_cast<int32_t>(len) + kSlotBytes;
  return free_gap - need;
}

}  // namespace

Err Heap::open(Pager* pager, const int32_t first_page) {
  pager_ = pager;
  first_page_ = first_page;
  hwm_page_ = first_page;
  free_bytes_.assign(static_cast<size_t>(pager_->page_count()), 0);
  // Rebuild the free space map by scanning the pages this heap owns.
  for (int32_t id = first_page; id < static_cast<int32_t>(pager_->page_count()); ++id) {
    char* page = nullptr;
    if (pager_->get(id, &page) != Err::Ok) return Err::IoError;
    free_bytes_[static_cast<size_t>(id)] = page_free(page, 0);
    hwm_page_ = id + 1;
  }
  return Err::Ok;
}

Err Heap::close() { return Err::Ok; }

Err Heap::free_space(const int32_t page_id, int32_t* bytes) {
  char* page = nullptr;
  if (pager_->get(page_id, &page) != Err::Ok) return Err::IoError;
  *bytes = free_bytes_[static_cast<size_t>(page_id)];
  return Err::Ok;
}

Err Heap::pick_page(const int32_t need, int32_t* page_id, bool* need_new_page) {
  for (int32_t id = first_page_; id < hwm_page_; ++id) {
    if (free_bytes_[static_cast<size_t>(id)] >= need) {
      *page_id = id;
      *need_new_page = false;
      return Err::Ok;
    }
  }
  *need_new_page = true;
  return Err::Ok;
}

Err Heap::insert(const char* payload, const int32_t len, Rid* rid) {
  const int32_t need = len + kSlotBytes;
  int32_t page_id = -1;
  bool need_new = false;
  if (pick_page(need, &page_id, &need_new) != Err::Ok) return Err::IoError;
  char* page = nullptr;
  if (need_new) {
    if (pager_->alloc(&page_id, &page) != Err::Ok) return Err::IoError;
    if (static_cast<size_t>(page_id) >= free_bytes_.size()) free_bytes_.resize(page_id + 1, 0);
    hwm_page_ = page_id + 1;
    free_bytes_[static_cast<size_t>(page_id)] = kPageSize - kHeaderBytes;
  } else if (pager_->get(page_id, &page) != Err::Ok) {
    return Err::IoError;
  }

  uint16_t slots = load_u16(page);
  // Find the first free slot entry or append one.
  int32_t slot = -1;
  for (uint16_t i = 0; i < slots; ++i) {
    if (load_u16(page + kHeaderBytes + i * kSlotBytes + 2) == 0) {
      slot = i;
      break;
    }
  }
  int32_t used_hi = kPageSize;
  for (uint16_t i = 0; i < slots; ++i) {
    const int32_t off = load_u16(page + kHeaderBytes + i * kSlotBytes);
    const int32_t l = load_u16(page + kHeaderBytes + i * kSlotBytes + 2);
    if (l != 0 && off < used_hi) used_hi = off;
  }
  const int32_t data_off = used_hi - len;
  const int32_t slots_end = kHeaderBytes + (static_cast<int32_t>(slots) + 1) * kSlotBytes;
  if (data_off < slots_end) return Err::TooManyRows;   // should not happen: pick_page reserved space
  if (slot < 0) {
    slot = slots;
    load_u16(page);  // no-op read to keep the load/store pair obvious
    store_u16(page, static_cast<uint16_t>(slots + 1));
  }
  store_u16(page + kHeaderBytes + slot * kSlotBytes, static_cast<uint16_t>(data_off));
  store_u16(page + kHeaderBytes + slot * kSlotBytes + 2, static_cast<uint16_t>(len));
  std::memcpy(page + data_off, payload, static_cast<size_t>(len));
  pager_->mark_dirty(page_id);
  free_bytes_[static_cast<size_t>(page_id)] = page_free(page, 0);
  rid->page = page_id;
  rid->slot = static_cast<int16_t>(slot);
  return Err::Ok;
}

Err Heap::read(const Rid& rid, char* out, const int32_t cap, int32_t* len) {
  const char* data = nullptr;
  int32_t l = 0;
  if (read_view(rid, &data, &l) != Err::Ok) return Err::IoError;
  if (l == 0) return Err::TableNotFound;   // dead slot
  if (l > cap) return Err::ValueOverflow;
  std::memcpy(out, data, static_cast<size_t>(l));
  *len = l;
  return Err::Ok;
}

Err Heap::read_view(const Rid& rid, const char** data, int32_t* len) {
  char* page = nullptr;
  if (pager_->get(rid.page, &page) != Err::Ok) return Err::IoError;
  const int32_t off = load_u16(page + kHeaderBytes + rid.slot * kSlotBytes);
  const uint16_t l = load_u16(page + kHeaderBytes + rid.slot * kSlotBytes + 2);
  *data = page + off;
  *len = l;
  return Err::Ok;
}

Err Heap::remove(const Rid& rid) {
  char* page = nullptr;
  if (pager_->get(rid.page, &page) != Err::Ok) return Err::IoError;
  store_u16(page + kHeaderBytes + rid.slot * kSlotBytes + 2, 0);
  pager_->mark_dirty(rid.page);
  free_bytes_[static_cast<size_t>(rid.page)] = page_free(page, 0);
  return Err::Ok;
}

Err Heap::update(const Rid& rid, const char* payload, const int32_t len, Rid* new_rid, bool* moved) {
  char* page = nullptr;
  if (pager_->get(rid.page, &page) != Err::Ok) return Err::IoError;
  const int32_t off = load_u16(page + kHeaderBytes + rid.slot * kSlotBytes);
  const uint16_t old_len = load_u16(page + kHeaderBytes + rid.slot * kSlotBytes + 2);
  if (old_len != 0 && len <= static_cast<int32_t>(old_len)) {
    std::memcpy(page + off, payload, static_cast<size_t>(len));
    store_u16(page + kHeaderBytes + rid.slot * kSlotBytes + 2, static_cast<uint16_t>(len));
    pager_->mark_dirty(rid.page);
    free_bytes_[static_cast<size_t>(rid.page)] = page_free(page, 0);
    *new_rid = rid;
    *moved = false;
    return Err::Ok;
  }
  // Grow: free the old slot and append the new payload.
  *moved = true;
  return insert(payload, len, new_rid);
}

Err Heap::live_rows(std::vector<Rid>* out) {
  out->clear();
  for (int32_t id = first_page_; id < hwm_page_; ++id) {
    char* page = nullptr;
    if (pager_->get(id, &page) != Err::Ok) return Err::IoError;
    const uint16_t slots = load_u16(page);
    for (uint16_t i = 0; i < slots; ++i) {
      const uint16_t l = load_u16(page + kHeaderBytes + i * kSlotBytes + 2);
      if (l != 0) {
        Rid r;
        r.page = id;
        r.slot = static_cast<int16_t>(i);
        out->push_back(r);
      }
    }
  }
  return Err::Ok;
}

}  // namespace mv
