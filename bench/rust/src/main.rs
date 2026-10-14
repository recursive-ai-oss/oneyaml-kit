//! In-process benchmark harness for Rust YAML libraries.
//!
//! `run` measures every (library, task, workload) cell that the workload set defines,
//! after the correctness guard has accepted what the library produced, and writes one
//! JSON line per cell. See docs/benchmarks.md for the protocol and the result format.

mod alloc;
mod guard;
mod libs;
mod measure;
mod model;
mod typed;

use model::{Library, Task, Workload};
use serde_json::{json, Value};
use std::io::Write;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};

#[global_allocator]
static ALLOC: alloc::Counting = alloc::Counting;

struct Args {
    cmd: String,
    workloads: PathBuf,
    out: Option<PathBuf>,
    mode: String,
    libraries: Vec<String>,
    tasks: Vec<String>,
    only: Vec<String>,
    skip: Vec<String>,
    file: Option<PathBuf>,
}

fn parse_args() -> Result<Args, String> {
    let mut it = std::env::args().skip(1);
    let cmd = it
        .next()
        .ok_or("usage: oneyaml-bench-rust <run|once|list|candidates> [options]")?;
    let mut a = Args {
        cmd,
        workloads: PathBuf::from("workloads"),
        out: None,
        mode: "quick".into(),
        libraries: vec![],
        tasks: vec![],
        only: vec![],
        skip: vec![],
        file: None,
    };
    while let Some(k) = it.next() {
        let mut val = || it.next().ok_or(format!("{k} needs a value"));
        match k.as_str() {
            "--workloads" => a.workloads = val()?.into(),
            "--out" => a.out = Some(val()?.into()),
            "--mode" => a.mode = val()?,
            "--library" => a.libraries.push(val()?),
            "--task" => a.tasks.push(val()?),
            "--workload" => a.only.push(val()?),
            "--skip" => a.skip.push(val()?),
            f if !f.starts_with("--") && a.file.is_none() => a.file = Some(f.into()),
            other => return Err(format!("unknown option {other}")),
        }
    }
    Ok(a)
}

fn load_workloads(dir: &Path, only: &[String]) -> Result<Vec<&'static Workload>, String> {
    let index: Value = serde_json::from_slice(
        &std::fs::read(dir.join("index.json"))
            .map_err(|e| format!("{}: {e}", dir.join("index.json").display()))?,
    )
    .map_err(|e| e.to_string())?;
    let mut out = vec![];
    for w in index["workloads"].as_array().into_iter().flatten() {
        let id = w["id"].as_str().unwrap_or_default();
        if !only.is_empty() && !only.iter().any(|o| o == id) {
            continue;
        }
        out.push(&*Box::leak(Box::new(Workload::load(&dir.join(id))?)));
    }
    Ok(out)
}

/// Reads bytes with two independent parsers; they must agree.
fn referee(bytes: &[u8]) -> Result<Value, String> {
    let a = libs::oneyaml::generic_json(bytes).map_err(|e| format!("oneyaml: {e}"))?;
    let b = libs::saphyr::generic_json(bytes).map_err(|e| format!("saphyr: {e}"))?;
    match guard::first_diff(&a, &b, "") {
        None => Ok(a),
        Some(d) => Err(format!("the two reference parsers disagree {d}")),
    }
}

fn panic_text(p: Box<dyn std::any::Any + Send>) -> String {
    p.downcast_ref::<&str>()
        .map(|s| s.to_string())
        .or_else(|| p.downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "panic".into())
}

fn harness_record(a: &Args, cfg: &measure::Config, libs: &[Box<dyn Library>]) -> Value {
    json!({
        "type": "harness",
        "name": "oneyaml-bench-rust",
        "version": env!("CARGO_PKG_VERSION"),
        "language": "rust",
        "runtime": env!("BENCH_RUSTC_VERSION"),
        "target": env!("BENCH_TARGET"),
        "flags": {"profile": env!("BENCH_PROFILE"), "opt_level": env!("BENCH_OPT_LEVEL"), "lto": "thin", "codegen_units": 1},
        "mode": a.mode,
        "config": {
            "warmup_ms": cfg.warmup.as_millis() as u64,
            "samples": cfg.samples,
            "sample_target_ms": cfg.sample_target.as_millis() as u64,
        },
        "timer": "std::time::Instant, batches of prepared iterations, median and median absolute deviation",
        "memory": "counting global allocator: peak live heap, bytes requested, allocation calls",
        "selftest_build": cfg!(feature = "selftest"),
        "libraries": libs.iter().map(|l| {
            let (pkg, ver) = l.package();
            json!({"id": l.id(), "package": pkg, "version": ver, "role": l.role().id(), "settings": l.settings()})
        }).collect::<Vec<_>>(),
    })
}

fn cell_name(l: &str, t: Task, w: &str) -> String {
    format!("{l}/{}/{w}", t.id())
}

fn one_cell(lib: &dyn Library, task: Task, w: &'static Workload, cfg: &measure::Config) -> Value {
    let base = json!({"library": lib.id(), "task": task.id(), "workload": w.id, "input_bytes": w.input.len()});
    let mut rec = base.as_object().cloned().unwrap_or_default();
    rec.insert("type".into(), "cell".into());
    let mut put = |status: &str, detail: &str| {
        rec.insert("status".into(), status.into());
        rec.insert("detail".into(), detail.into());
    };
    let runner = match catch_unwind(AssertUnwindSafe(|| lib.runner(task, w))) {
        Ok(Ok(r)) => r,
        Ok(Err(model::Unsupported(why))) => {
            put("unsupported", why);
            return Value::Object(rec);
        }
        Err(p) => {
            put(
                "error",
                &format!("panic while preparing: {}", panic_text(p)),
            );
            return Value::Object(rec);
        }
    };
    // Guard first: run the task once and judge the answer. A wrong answer gets no number.
    let answer = catch_unwind(AssertUnwindSafe(|| {
        (runner.check(), runner.check_failing())
    }));
    let (answer, failing) = match answer {
        Ok(x) => x,
        Err(p) => (Err(format!("panic: {}", panic_text(p))), None),
    };
    let verdict = guard::judge(task, w, answer, failing, &referee);
    put(
        verdict.status(),
        match &verdict {
            guard::Verdict::Ok { .. } => "",
            guard::Verdict::Differs(d) | guard::Verdict::Rejects(d) => d,
        },
    );
    if let guard::Verdict::Ok { variant } = &verdict {
        rec.insert("variant".into(), variant.clone().into());
        let measured = catch_unwind(AssertUnwindSafe(|| {
            (measure::time(&*runner, cfg), measure::memory(&*runner))
        }));
        match measured {
            Ok((t, m)) => {
                rec.insert(
                    "time".into(),
                    json!({
                        "iters_per_sample": t.iters_per_sample,
                        "samples": t.samples_ns.len(),
                        "median_ns": t.median(),
                        "mad_ns": t.mad(),
                        "min_ns": t.samples_ns.first(),
                        "max_ns": t.samples_ns.last(),
                        "samples_ns": t.samples_ns,
                    }),
                );
                rec.insert(
                    "memory".into(),
                    json!({
                        "peak_bytes": m.peak_bytes,
                        "allocated_bytes": m.allocated_bytes,
                        "allocations": m.allocations,
                    }),
                );
            }
            Err(p) => {
                rec.insert("status".into(), "error".into());
                rec.insert(
                    "detail".into(),
                    format!("panic while measuring: {}", panic_text(p)).into(),
                );
            }
        }
    }
    Value::Object(rec)
}

fn run(a: &Args) -> Result<(), String> {
    let cfg = match a.mode.as_str() {
        "quick" => measure::Config::quick(),
        "full" => measure::Config::full(),
        m => return Err(format!("unknown mode {m}")),
    };
    let workloads = load_workloads(&a.workloads, &a.only)?;
    let libs: Vec<Box<dyn Library>> = libs::all()
        .into_iter()
        .filter(|l| a.libraries.is_empty() || a.libraries.iter().any(|x| x == l.id()))
        .collect();
    let mut out: Box<dyn Write> = match &a.out {
        Some(p) => Box::new(std::io::BufWriter::new(
            std::fs::File::create(p).map_err(|e| e.to_string())?,
        )),
        None => Box::new(std::io::stdout()),
    };
    let mut emit = |v: &Value| -> Result<(), String> {
        writeln!(out, "{v}")
            .and_then(|()| out.flush())
            .map_err(|e| e.to_string())
    };
    emit(&harness_record(a, &cfg, &libs))?;
    std::panic::set_hook(Box::new(|_| {}));
    for w in &workloads {
        for task in w
            .tasks
            .iter()
            .copied()
            .filter(|t| a.tasks.is_empty() || a.tasks.iter().any(|x| x == t.id()))
        {
            for lib in &libs {
                let name = cell_name(lib.id(), task, &w.id);
                if a.skip.contains(&name) {
                    continue;
                }
                // A crash cannot be caught, so mark the cell first; the driver reads the
                // last mark to find the cell that killed the process.
                emit(&json!({"type": "begin", "cell": name}))?;
                emit(&one_cell(lib.as_ref(), task, w, &cfg))?;
            }
        }
    }
    Ok(())
}

/// Runs one cell for instruction counting: no guard, no clock, one iteration.
fn once(a: &Args) -> Result<(), String> {
    let (Some(lib), Some(task), Some(wl)) = (a.libraries.first(), a.tasks.first(), a.only.first())
    else {
        return Err("once needs --library, --task and --workload".into());
    };
    let task = Task::parse(task).ok_or("unknown task")?;
    let w = load_workloads(&a.workloads, std::slice::from_ref(wl))?
        .pop()
        .ok_or("unknown workload")?;
    let libs = libs::all();
    let lib = libs
        .iter()
        .find(|l| l.id() == lib)
        .ok_or("unknown library")?;
    let r = lib.runner(task, w).map_err(|u| u.0.to_string())?;
    let mut b = r.prepare(1);
    measure::oneyaml_bench_region(&mut *b);
    Ok(())
}

/// Prints what the two reference parsers read from a file, for freezing an expectation.
fn candidates(a: &Args) -> Result<(), String> {
    let f = a.file.as_ref().ok_or("candidates needs a file")?;
    let bytes = std::fs::read(f).map_err(|e| e.to_string())?;
    let show = |r: Result<Value, String>| match r {
        Ok(v) => json!({"value": v}),
        Err(e) => json!({"error": e}),
    };
    let out = json!({
        "oneyaml": show(libs::oneyaml::generic_json(&bytes)),
        "saphyr": show(libs::saphyr::generic_json(&bytes)),
    });
    println!("{out}");
    Ok(())
}

fn list(a: &Args) -> Result<(), String> {
    for l in libs::all() {
        let (p, v) = l.package();
        println!("{} {p} {v} {}", l.id(), l.role().id());
    }
    let _ = a;
    Ok(())
}

fn main() {
    let result = parse_args().and_then(|a| match a.cmd.as_str() {
        "run" => run(&a),
        "once" => once(&a),
        "candidates" => candidates(&a),
        "list" => list(&a),
        c => Err(format!("unknown command {c}")),
    });
    if let Err(e) = result {
        eprintln!("oneyaml-bench-rust: {e}");
        std::process::exit(2);
    }
}

#[cfg(all(test, feature = "selftest"))]
mod control_tests {
    //! The guard must accept a correct answer and refuse a wrong one for every task, and a
    //! refused cell must carry no numbers. These run against fake libraries that return the
    //! expectation, a slightly altered expectation, and an error.
    use super::*;
    use crate::libs::control::Control;
    use serde_json::json;

    fn tiny() -> &'static Workload {
        let input: &'static [u8] = b"a: 1\nb: [x, y]\n";
        Box::leak(Box::new(Workload {
            id: "tiny".into(),
            input,
            input_str: "a: 1\nb: [x, y]\n",
            documents: 1,
            tasks: Task::ALL.to_vec(),
            typed_model: Some("records".into()),
            expected: vec![("spec".into(), json!({"a": 1, "b": ["x", "y"]}))],
            edit_ops: vec![],
            edit_expected: vec![("spec".into(), b"a: 2\nb: [x, y]\n")],
            patch_ops: vec![],
            patch_failing_ops: vec![],
            patch_expected: vec![("spec".into(), b"a: 2\nb: [x, y]\n")],
            meta: Value::Null,
        }))
    }

    #[test]
    fn controls_are_judged_on_every_task() {
        let cfg = measure::Config::quick();
        for task in Task::ALL {
            let right = one_cell(&Control::Correct, task, tiny(), &cfg);
            assert_eq!(right["status"], "ok", "{task:?}: {right}");
            assert!(right["time"]["median_ns"].as_f64().unwrap_or(0.0) > 0.0);
            assert!(right["memory"].is_object());

            let wrong = one_cell(&Control::Wrong, task, tiny(), &cfg);
            assert_eq!(wrong["status"], "differs", "{task:?}: {wrong}");
            assert!(
                wrong.get("time").is_none() && wrong.get("memory").is_none(),
                "a wrong answer got numbers"
            );
            assert!(!wrong["detail"].as_str().unwrap_or("").is_empty());

            let rej = one_cell(&Control::Reject, task, tiny(), &cfg);
            assert_eq!(rej["status"], "rejects", "{task:?}: {rej}");
            assert!(rej.get("time").is_none());
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn referee_agrees_on_plain_yaml() {
        let v = super::referee(b"a: [1, 2.5, x, null, true]\n").unwrap();
        assert_eq!(v, serde_json::json!({"a": [1, 2.5, "x", null, true]}));
    }

    #[test]
    fn referee_reports_unparseable_text() {
        assert!(super::referee(b"a: [1, 2\n").is_err());
    }
}
