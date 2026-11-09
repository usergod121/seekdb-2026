#include "storage/pager.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include <cstdlib>
#include <cstring>

namespace mv {

namespace {

bool pread_all(const int fd, void* buf, size_t n, off_t off) {
  char* p = static_cast<char*>(buf);
  while (n > 0) {
    const ssize_t r = ::pread(fd, p, n, off);
    if (r < 0) {
      if (errno == EINTR) continue;
      return false;
    }
    if (r == 0) return false;
    p += r;
    n -= static_cast<size_t>(r);
    off += r;
  }
  return true;
}

bool pwrite_all(const int fd, const void* buf, size_t n, off_t off) {
  const char* p = static_cast<const char*>(buf);
  while (n > 0) {
    const ssize_t w = ::pwrite(fd, p, n, off);
    if (w < 0) {
      if (errno == EINTR) continue;
      return false;
    }
    p += w;
    n -= static_cast<size_t>(w);
    off += w;
  }
  return true;
}

}  // namespace

Pager::~Pager() {
  for (Frame* f : free_frames_) {
    std::free(f->data);
    delete f;
  }
}

Err Pager::open(const std::string& path, const int64_t pool_pages) {
  if (fd_ >= 0) return Err::IoError;
  fd_ = ::open(path.c_str(), O_CREAT | O_RDWR, 0644);
  if (fd_ < 0) return Err::IoError;
  struct stat st;
  if (::fstat(fd_, &st) != 0) return Err::IoError;
  if (st.st_size % kPageSize != 0) return Err::IoError;  // a torn file is not silently accepted
  page_count_ = st.st_size / kPageSize;
  pool_pages_ = (pool_pages > 0) ? pool_pages : 256;
  const int64_t prealloc = (page_count_ < pool_pages_) ? page_count_ : pool_pages_;
  free_frames_.reserve(static_cast<size_t>(pool_pages_));
  for (int64_t i = 0; i < prealloc; ++i) {
    Frame* f = new Frame();
    f->data = static_cast<char*>(std::malloc(kPageSize));
    if (f->data == nullptr) return Err::IoError;
    free_frames_.push_back(f);
  }
  return Err::Ok;
}

Err Pager::close() {
  if (fd_ < 0) return Err::Ok;
  const Err rc = flush();
  ::close(fd_);
  fd_ = -1;
  return rc;
}

Err Pager::write_back(Frame* f) {
  if (!f->dirty) return Err::Ok;
  if (!pwrite_all(fd_, f->data, kPageSize, static_cast<off_t>(f->page_id) * kPageSize)) {
    return Err::IoError;
  }
  f->dirty = false;
  return Err::Ok;
}

Pager::Frame* Pager::pick_victim(Err* err) {
  Frame* victim = nullptr;
  uint64_t best = UINT64_MAX;
  for (auto& kv : frames_) {
    Frame* f = kv.second;
    if (f->last_used < best) {
      best = f->last_used;
      victim = f;
    }
  }
  if (victim == nullptr) {
    // Nothing resident yet: take a free frame, or create one if the pool has not reached its bound.
    if (!free_frames_.empty()) {
      victim = free_frames_.back();
      free_frames_.pop_back();
    } else {
      Frame* f = new Frame();
      f->data = static_cast<char*>(std::malloc(kPageSize));
      if (f->data == nullptr) {
        *err = Err::IoError;
        return nullptr;
      }
      victim = f;
    }
    return victim;
  }
  if (write_back(victim) != Err::Ok) {
    *err = Err::IoError;
    return nullptr;
  }
  frames_.erase(victim->page_id);
  return victim;
}

Pager::Frame* Pager::find_or_load(const int32_t page_id, Err* err) {
  auto it = frames_.find(page_id);
  if (it != frames_.end()) {
    it->second->last_used = ++clock_;
    return it->second;
  }
  if (page_id < 0 || page_id >= page_count_) {
    *err = Err::IoError;
    return nullptr;
  }
  Frame* f = pick_victim(err);
  if (f == nullptr) return nullptr;
  if (!pread_all(fd_, f->data, kPageSize, static_cast<off_t>(page_id) * kPageSize)) {
    *err = Err::IoError;
    return nullptr;
  }
  f->page_id = page_id;
  f->dirty = false;
  f->last_used = ++clock_;
  frames_[page_id] = f;
  return f;
}

Err Pager::get(const int32_t page_id, char** page) {
  Err err = Err::Ok;
  Frame* f = find_or_load(page_id, &err);
  if (f == nullptr) return (err == Err::Ok) ? Err::IoError : err;
  *page = f->data;
  return Err::Ok;
}

Err Pager::alloc(int32_t* page_id, char** page) {
  const int32_t id = static_cast<int32_t>(page_count_);
  // Extend the file so a reader of the file size agrees with page_count_ even before the page is
  // written back.
  char zero[kPageSize];
  std::memset(zero, 0, sizeof(zero));
  if (!pwrite_all(fd_, zero, kPageSize, static_cast<off_t>(id) * kPageSize)) return Err::IoError;
  ++page_count_;

  Err err = Err::Ok;
  Frame* f = pick_victim(&err);
  if (f == nullptr) return (err == Err::Ok) ? Err::IoError : err;
  std::memset(f->data, 0, kPageSize);
  f->page_id = id;
  f->dirty = true;
  f->last_used = ++clock_;
  frames_[id] = f;
  *page_id = id;
  *page = f->data;
  return Err::Ok;
}

void Pager::mark_dirty(const int32_t page_id) {
  auto it = frames_.find(page_id);
  if (it != frames_.end()) it->second->dirty = true;
}

Err Pager::flush() {
  for (auto& kv : frames_) {
    if (kv.second->dirty) {
      if (write_back(kv.second) != Err::Ok) return Err::IoError;
    }
  }
  if (fd_ >= 0 && ::fsync(fd_) != 0) return Err::IoError;
  return Err::Ok;
}

}  // namespace mv
