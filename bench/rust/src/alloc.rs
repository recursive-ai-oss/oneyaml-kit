//! A counting allocator for the memory columns.
//!
//! Tracking is off while time is measured, so the timed runs pay one relaxed load per
//! allocation and nothing else. The memory pass switches it on for a single run of the
//! task and reads the high-water mark of live heap bytes, the bytes requested and the
//! number of allocations. Every Rust library goes through the global allocator, so this
//! sees all of their heap use; it does not see stacks or memory mapped outside it.

use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering::Relaxed};

pub struct Counting;

static TRACK: AtomicBool = AtomicBool::new(false);
static LIVE: AtomicI64 = AtomicI64::new(0);
static PEAK: AtomicI64 = AtomicI64::new(0);
static ALLOCATED: AtomicU64 = AtomicU64::new(0);
static COUNT: AtomicU64 = AtomicU64::new(0);

/// What one tracked run used.
#[derive(Clone, Copy, Debug, Default)]
pub struct Usage {
    /// High-water mark of live heap bytes since tracking started, including the state
    /// the task starts from.
    pub peak_bytes: u64,
    /// Bytes requested from the allocator by the task itself.
    pub allocated_bytes: u64,
    /// Allocation calls made by the task itself.
    pub allocations: u64,
    /// What the run left live: the live heap after the run, less the live heap just before it, with
    /// the starting state held in both readings. Memory is freed the moment it is dropped, so there is no
    /// collection to wait for; this is the same quantity the other harnesses report.
    pub retained_bytes: u64,
}

fn grow(by: i64) {
    let now = LIVE.fetch_add(by, Relaxed) + by;
    PEAK.fetch_max(now, Relaxed);
}

// SAFETY: every method forwards to the system allocator with the arguments it was given;
// the counters are atomics and never allocate.
unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        let p = System.alloc(l);
        if TRACK.load(Relaxed) && !p.is_null() {
            grow(l.size() as i64);
            ALLOCATED.fetch_add(l.size() as u64, Relaxed);
            COUNT.fetch_add(1, Relaxed);
        }
        p
    }
    unsafe fn alloc_zeroed(&self, l: Layout) -> *mut u8 {
        let p = System.alloc_zeroed(l);
        if TRACK.load(Relaxed) && !p.is_null() {
            grow(l.size() as i64);
            ALLOCATED.fetch_add(l.size() as u64, Relaxed);
            COUNT.fetch_add(1, Relaxed);
        }
        p
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        if TRACK.load(Relaxed) {
            LIVE.fetch_sub(l.size() as i64, Relaxed);
        }
        System.dealloc(p, l);
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, new: usize) -> *mut u8 {
        let q = System.realloc(p, l, new);
        if TRACK.load(Relaxed) && !q.is_null() {
            grow(new as i64 - l.size() as i64);
            ALLOCATED.fetch_add(new as u64, Relaxed);
            COUNT.fetch_add(1, Relaxed);
        }
        q
    }
}

/// Live heap bytes right now, relative to when tracking started. Only meaningful while tracking.
pub fn live() -> i64 {
    LIVE.load(Relaxed)
}

/// Starts tracking from zero.
pub fn start() {
    LIVE.store(0, Relaxed);
    PEAK.store(0, Relaxed);
    ALLOCATED.store(0, Relaxed);
    COUNT.store(0, Relaxed);
    TRACK.store(true, Relaxed);
}

/// Forgets the requests counted so far but keeps the live level and the high-water mark,
/// so set-up work is not charged to the task while its state still counts as live.
pub fn reset_requests() {
    ALLOCATED.store(0, Relaxed);
    COUNT.store(0, Relaxed);
}

/// Stops tracking and returns what was seen. `retained_bytes` is left for the caller to fill in, because
/// only it knows where the run began and ended.
pub fn stop() -> Usage {
    TRACK.store(false, Relaxed);
    Usage {
        peak_bytes: PEAK.load(Relaxed).max(0) as u64,
        allocated_bytes: ALLOCATED.load(Relaxed),
        allocations: COUNT.load(Relaxed),
        retained_bytes: 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counts_a_known_allocation() {
        start();
        let v: Vec<u8> = Vec::with_capacity(4096);
        let u = stop();
        drop(v);
        // Other test threads may allocate too, so the figures are lower bounds.
        assert!(u.peak_bytes >= 4096 && u.allocated_bytes >= 4096 && u.allocations >= 1);
    }
}
