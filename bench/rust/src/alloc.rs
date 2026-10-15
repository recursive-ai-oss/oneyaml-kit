//! A counting global allocator: bytes requested and allocation calls, only while tracking is on.

use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::Relaxed};

pub struct Counting;

static TRACK: AtomicBool = AtomicBool::new(false);
static ALLOCATED: AtomicU64 = AtomicU64::new(0);
static COUNT: AtomicU64 = AtomicU64::new(0);

/// What one tracked run requested from the allocator.
#[derive(Clone, Copy, Debug, Default)]
pub struct Usage {
    pub allocated_bytes: u64,
    pub allocations: u64,
}

fn count(bytes: usize) {
    ALLOCATED.fetch_add(bytes as u64, Relaxed);
    COUNT.fetch_add(1, Relaxed);
}

// SAFETY: every method forwards to the system allocator with the arguments it was given;
// the counters are atomics and never allocate.
unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        let p = System.alloc(l);
        if TRACK.load(Relaxed) && !p.is_null() {
            count(l.size());
        }
        p
    }
    unsafe fn alloc_zeroed(&self, l: Layout) -> *mut u8 {
        let p = System.alloc_zeroed(l);
        if TRACK.load(Relaxed) && !p.is_null() {
            count(l.size());
        }
        p
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        System.dealloc(p, l);
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, new: usize) -> *mut u8 {
        let q = System.realloc(p, l, new);
        if TRACK.load(Relaxed) && !q.is_null() {
            count(new);
        }
        q
    }
}

/// Counts what `f` requests, and nothing before it.
pub fn track<R>(f: impl FnOnce() -> R) -> (R, Usage) {
    ALLOCATED.store(0, Relaxed);
    COUNT.store(0, Relaxed);
    TRACK.store(true, Relaxed);
    let r = f();
    TRACK.store(false, Relaxed);
    (
        r,
        Usage {
            allocated_bytes: ALLOCATED.load(Relaxed),
            allocations: COUNT.load(Relaxed),
        },
    )
}
