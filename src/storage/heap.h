// Slotted-page heap file on top of the Pager: variable length rows with a stable (page, slot) address.
//
// Page layout (kPageSize bytes):
//   [u16 slot_count][u16 reserved][u32 reserved2]        8 byte header
//   slot array: slot i at offset 8 + 4*i, entry = [u16 offset][u16 len]   (len == 0 marks a dead slot)
//   row payloads grow downwards from the end of the page
#pragma once

#include <cstdint>
#include <vector>

#include "common.h"
#include "storage/pager.h"

namespace mv {

struct Rid {
  int32_t page = -1;
  int16_t slot = -1;
  bool valid() const { return page >= 0 && slot >= 0; }
};

class Heap {
 public:
  // `first_page` is the first page owned by this heap; everything up to it belongs to the catalog.
  Err open(Pager* pager, int32_t first_page);
  Err close();

  Err insert(const char* payload, int32_t len, Rid* rid);
  // Copies the row into `out` (capacity `cap`); returns Err::ValueOverflow when it does not fit.
  Err read(const Rid& rid, char* out, int32_t cap, int32_t* len);
  Err read_view(const Rid& rid, const char** data, int32_t* len);   // valid until the next heap call
  Err remove(const Rid& rid);
  // Overwrites in place when the payload fits the slot, otherwise appends and returns a new address.
  Err update(const Rid& rid, const char* payload, int32_t len, Rid* new_rid, bool* moved);

  // Live row addresses, in page/slot order.
  Err live_rows(std::vector<Rid>* out);
  // The first page that is free for a new heap (for catalog bookkeeping).
  int32_t hwm_page() const { return hwm_page_; }

 private:
  Err free_space(const int32_t page_id, int32_t* bytes);
  Err pick_page(const int32_t need, int32_t* page_id, bool* need_new_page);

  Pager* pager_ = nullptr;
  int32_t first_page_ = 0;
  int32_t hwm_page_ = 0;
  std::vector<int32_t> free_bytes_;   // per page: free space inside the page
};

}  // namespace mv
