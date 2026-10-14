//! Controls: fake libraries that prove the guard bites. They exist only with the
//! `selftest` feature, so a measurement build can never include one.
//!
//! `Correct` returns the expectation itself (the guard must accept it), `Wrong` returns
//! the expectation with one small change (the guard must refuse it and no time may be
//! recorded), `Reject` refuses every input, and `Crash` aborts the process on a generic
//! load.

use crate::model::*;
use serde_json::Value;

#[derive(Clone, Copy)]
pub enum Control {
    Correct,
    Wrong,
    Reject,
    /// Kills the process, to prove the driver survives a crash and names the cell.
    Crash,
}

/// Changes the first leaf of a value, so a comparison that looks at too little misses it.
fn perturb(v: &Value) -> Value {
    match v {
        Value::String(s) => Value::String(format!("{s}!")),
        Value::Number(n) => Value::from(n.as_f64().unwrap_or(0.0) + 1.0),
        Value::Bool(b) => Value::Bool(!b),
        Value::Null => Value::Bool(false),
        Value::Array(a) if !a.is_empty() => {
            let mut a = a.clone();
            let last = a.len() - 1;
            a[last] = perturb(&a[last]);
            Value::Array(a)
        }
        Value::Object(m) if !m.is_empty() => {
            let mut m = m.clone();
            let k = m.keys().next_back().cloned().unwrap_or_default();
            if let Some(x) = m.get_mut(&k) {
                *x = perturb(x);
            }
            Value::Object(m)
        }
        other => other.clone(),
    }
}

fn tweak(b: &[u8]) -> Vec<u8> {
    let mut v = b.to_vec();
    v.push(b' ');
    v
}

impl Library for Control {
    fn id(&self) -> &'static str {
        match self {
            Control::Correct => "control-correct",
            Control::Wrong => "control-wrong",
            Control::Reject => "control-reject",
            Control::Crash => "control-crash",
        }
    }
    fn package(&self) -> (&'static str, &'static str) {
        ("control", "0")
    }
    fn role(&self) -> Role {
        Role::Control
    }
    fn runner(&self, task: Task, w: &'static Workload) -> Result<Box<dyn Runner>, Unsupported> {
        let me = *self;
        if matches!(me, Control::Crash) && task == Task::GenericLoad {
            return Ok(runner(
                || (),
                |()| -> Result<(), String> { std::process::abort() },
                |()| Product::Bytes(vec![]),
            ));
        }
        let value = move || match me {
            Control::Wrong => perturb(w.base_value()),
            _ => w.base_value().clone(),
        };
        let bytes = move |b: &'static [u8]| match me {
            Control::Wrong => tweak(b),
            _ => b.to_vec(),
        };
        let fail = move || -> Result<(), String> {
            if matches!(me, Control::Reject) {
                Err("control rejects everything".into())
            } else {
                Ok(())
            }
        };
        Ok(match task {
            Task::TypedLoad | Task::GenericLoad => {
                if task == Task::TypedLoad && w.typed_model.is_none() {
                    return Err(Unsupported("no typed model"));
                }
                runner(|| (), move |()| fail().map(|()| value()), Product::Value)
            }
            Task::Dump => runner(
                || (),
                move |()| fail().map(|()| value().to_string().into_bytes()),
                Product::Bytes,
            ),
            Task::DocumentParse => runner(
                || (),
                move |()| fail().map(|()| (value(), bytes(w.input))),
                |(v, b)| Product::Document {
                    value: Some(v),
                    emitted: b,
                },
            ),
            Task::UnchangedEmit => runner(
                || (),
                move |()| fail().map(|()| bytes(w.input)),
                Product::Bytes,
            ),
            Task::EditScript => {
                let want = w
                    .edit_expected
                    .first()
                    .ok_or(Unsupported("no edit script"))?
                    .1;
                runner(
                    || (),
                    move |()| fail().map(|()| bytes(want)),
                    Product::Bytes,
                )
            }
            Task::Patch => {
                let want = w.patch_expected.first().ok_or(Unsupported("no patch"))?.1;
                patch_runner(
                    || (),
                    move |()| fail().map(|()| bytes(want)),
                    Product::Bytes,
                    move || FailedPatch {
                        errored: !matches!(me, Control::Wrong),
                        bytes: w.input.to_vec(),
                    },
                )
            }
        })
    }
}
