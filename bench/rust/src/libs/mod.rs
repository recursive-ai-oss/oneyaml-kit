//! The libraries under measurement. Each module implements `Library` for one crate,
//! using the entry points its own documentation shows for the task.

#[cfg(feature = "selftest")]
pub mod control;
pub mod oneyaml;
pub mod saphyr;
pub mod serde_yaml_ng;
pub mod yaml_edit;

use crate::model::{runner, Library, Op, Product, Runner, Seg, Workload};
use serde_json::Value;

/// Every library, own first.
pub fn all() -> Vec<Box<dyn Library>> {
    #[allow(unused_mut)]
    let mut v: Vec<Box<dyn Library>> = vec![
        Box::new(oneyaml::OneYaml),
        Box::new(serde_yaml_ng::SerdeYamlNg),
        Box::new(saphyr::Saphyr),
        Box::new(yaml_edit::YamlEdit),
    ];
    #[cfg(feature = "selftest")]
    {
        v.push(Box::new(control::Control::Correct));
        v.push(Box::new(control::Control::Wrong));
        v.push(Box::new(control::Control::Reject));
        v.push(Box::new(control::Control::Crash));
    }
    v
}

/// The common JSON form of a key: its scalar text.
pub fn key_text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// One edit with its pointer already resolved against the workload, so the timed work
/// does no path parsing of the harness's own.
pub struct Step {
    pub op: String,
    pub segs: Vec<Seg>,
    /// The path in the dotted dialect of the library being driven.
    pub dotted: String,
    pub value: Option<Value>,
}

/// Resolves the workload's edit pointers once. A token is an index where the value there
/// is a sequence and a key where it is a mapping; `escape` and `index` spell them in the
/// library's own path syntax.
pub fn resolve_steps(
    w: &Workload,
    ops: &[Op],
    escape: fn(&str) -> String,
    index: fn(usize) -> String,
) -> Result<Vec<Step>, String> {
    ops.iter()
        .map(|op| {
            let segs = crate::model::pointer_segments(w.base_value(), &op.path, false)?;
            let mut dotted = String::new();
            for seg in &segs {
                match seg {
                    Seg::Key(k) => {
                        if !dotted.is_empty() {
                            dotted.push('.');
                        }
                        dotted.push_str(&escape(k));
                    }
                    Seg::Index(i) => dotted.push_str(&index(*i)),
                }
            }
            Ok(Step {
                op: op.op.clone(),
                segs,
                dotted,
                value: op.value.clone(),
            })
        })
        .collect()
}

/// A runner for a task whose set-up could not be done: the cell reports the reason
/// instead of a number.
pub fn failing_runner(msg: String) -> Box<dyn Runner> {
    runner(
        || (),
        move |_| Err::<(), String>(msg.clone()),
        |()| Product::Bytes(vec![]),
    )
}
