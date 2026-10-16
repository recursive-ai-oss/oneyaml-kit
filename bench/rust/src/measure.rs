//! Time and allocation of one cell.
//!
//! Time is criterion's, configured the way its documentation shortens a slow benchmark: a 1 s
//! warm-up, 2 s of measurement and its smallest sample size of 10 (quick mode shortens the warm-up
//! and measurement time further). Criterion's defaults of 3 s, 5 s and 100 samples cost tens of seconds per
//! cell on the large inputs. Each starting state is built outside the clock and dropped after it.
//! Allocation is one separate run through the counting allocator, so counting never slows the timed ones.

use crate::alloc;
use crate::model::Runner;
use criterion::{BatchSize, Criterion};
use std::path::Path;
use std::time::Duration;

pub struct Timing {
    /// Criterion's mean and median time of one run, in nanoseconds.
    pub mean_ns: f64,
    pub median_ns: f64,
}

fn estimate(path: &Path, which: &str) -> Result<f64, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let v: serde_json::Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    v[which]["point_estimate"]
        .as_f64()
        .ok_or_else(|| format!("no {which} estimate in {}", path.display()))
}

/// Times the task with criterion; `dir` is where criterion keeps its files.
pub fn time(r: &dyn Runner, quick: bool, dir: &Path) -> Result<Timing, String> {
    let mut c = Criterion::default()
        .output_directory(dir)
        .with_output_color(false);
    c = if quick {
        c.warm_up_time(Duration::from_millis(20))
            .measurement_time(Duration::from_millis(100))
    } else {
        c.warm_up_time(Duration::from_secs(1))
            .measurement_time(Duration::from_secs(2))
    }
    .sample_size(10);
    {
        let mut g = c.benchmark_group("cell");
        g.bench_function("run", |b| {
            b.iter_batched_ref(
                || r.prepare(1),
                |batch| batch.run(),
                BatchSize::PerIteration,
            )
        });
        g.finish();
    }
    let file = dir
        .join("cell")
        .join("run")
        .join("new")
        .join("estimates.json");
    Ok(Timing {
        mean_ns: estimate(&file, "mean")?,
        median_ns: estimate(&file, "median")?,
    })
}

/// One counted run: the bytes requested and allocation calls the task itself made.
pub fn memory(r: &dyn Runner) -> alloc::Usage {
    let mut b = r.prepare(1);
    let ((), u) = alloc::track(|| b.run());
    drop(b);
    u
}
