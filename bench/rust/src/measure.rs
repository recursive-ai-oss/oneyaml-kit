//! Wall-clock and memory measurement of one cell.
//!
//! Time: warm up, then take `samples` samples. Each sample runs a batch of `iters`
//! prepared iterations and divides the elapsed time by `iters`; `iters` is chosen so a
//! sample lasts about `sample_target`. The spread is the median absolute deviation of the
//! samples. Memory is one separate, tracked run, so tracking never slows the timed ones.

use crate::alloc;
use crate::model::{Batch, Runner};
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug)]
pub struct Config {
    pub warmup: Duration,
    pub samples: usize,
    pub sample_target: Duration,
    /// Upper bound on the heap held by the prepared states of one batch.
    pub max_prepared_bytes: i64,
    pub max_iters: usize,
}

impl Config {
    pub fn quick() -> Config {
        Config {
            warmup: Duration::from_millis(2),
            samples: 5,
            sample_target: Duration::from_millis(1),
            max_prepared_bytes: 64 << 20,
            max_iters: 10_000,
        }
    }
    pub fn full() -> Config {
        Config {
            warmup: Duration::from_millis(300),
            samples: 31,
            sample_target: Duration::from_millis(50),
            max_prepared_bytes: 512 << 20,
            max_iters: 1_000_000,
        }
    }
}

#[derive(Clone, Debug)]
pub struct Timing {
    pub iters_per_sample: usize,
    /// Per-iteration time of each sample, sorted.
    pub samples_ns: Vec<f64>,
}

impl Timing {
    pub fn median(&self) -> f64 {
        median_sorted(&self.samples_ns)
    }
    /// Median absolute deviation: the spread that one slow sample cannot inflate.
    pub fn mad(&self) -> f64 {
        let m = self.median();
        let mut d: Vec<f64> = self.samples_ns.iter().map(|x| (x - m).abs()).collect();
        d.sort_by(|a, b| a.total_cmp(b));
        median_sorted(&d)
    }
}

pub fn median_sorted(v: &[f64]) -> f64 {
    if v.is_empty() {
        return 0.0;
    }
    let n = v.len();
    if n % 2 == 1 {
        v[n / 2]
    } else {
        (v[n / 2 - 1] + v[n / 2]) / 2.0
    }
}

/// The heap one prepared state holds, measured on a throw-away state.
fn state_bytes(r: &dyn Runner) -> i64 {
    alloc::start();
    let b = r.prepare(1);
    let held = alloc::live();
    alloc::stop();
    drop(b);
    held.max(1)
}

pub fn time(r: &dyn Runner, cfg: &Config) -> Timing {
    let cap = ((cfg.max_prepared_bytes / state_bytes(r)).max(1) as usize).min(cfg.max_iters);
    // Warm up and calibrate on single iterations, keeping the fastest to ignore a cold start.
    let mut one = f64::MAX;
    let start = Instant::now();
    let mut tries = 0;
    while tries < 3 || (start.elapsed() < cfg.warmup && tries < 1_000) {
        let mut b = r.prepare(1);
        let t = Instant::now();
        b.run();
        one = one.min(t.elapsed().as_nanos() as f64);
        tries += 1;
    }
    let iters =
        ((cfg.sample_target.as_nanos() as f64 / one.max(1.0)).ceil() as usize).clamp(1, cap);
    let mut samples = Vec::with_capacity(cfg.samples);
    for _ in 0..cfg.samples {
        let mut b = r.prepare(iters);
        let t = Instant::now();
        b.run();
        samples.push(t.elapsed().as_nanos() as f64 / iters as f64);
    }
    samples.sort_by(|a, b| a.total_cmp(b));
    Timing {
        iters_per_sample: iters,
        samples_ns: samples,
    }
}

/// One tracked run: peak live heap (including the state the task starts from), bytes
/// requested and allocation calls made by the task itself.
pub fn memory(r: &dyn Runner) -> alloc::Usage {
    alloc::start();
    let mut b = r.prepare(1);
    alloc::reset_requests();
    b.run();
    let u = alloc::stop();
    drop(b);
    u
}

/// The only region instruction counting looks at (`--once` under callgrind).
#[inline(never)]
#[no_mangle]
pub fn oneyaml_bench_region(b: &mut dyn Batch) {
    b.run();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn median_and_spread() {
        assert_eq!(median_sorted(&[1.0, 2.0, 9.0]), 2.0);
        assert_eq!(median_sorted(&[1.0, 2.0, 3.0, 10.0]), 2.5);
        let t = Timing {
            iters_per_sample: 1,
            samples_ns: vec![10.0, 10.0, 10.0, 20.0, 30.0],
        };
        assert_eq!(t.median(), 10.0);
        assert_eq!(t.mad(), 0.0);
    }
}
