//! The libraries under measurement. Each module implements `Library` for one crate,
//! using the entry points its own documentation shows for the task.

#[cfg(feature = "selftest")]
pub mod control;
pub mod oneyaml;
pub mod saphyr;
pub mod serde_yaml_ng;
pub mod yaml_edit;

use crate::model::{Library, Op, Seg, Workload};
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

/// A dotted path (`a.b[0].c` or `a.b.0.c`, as the library spells it), resolving which
/// tokens are indices from the workload's own value.
pub fn dotted_path(
    w: &Workload,
    op: &Op,
    escape: fn(&str) -> String,
    index: fn(usize) -> String,
) -> Result<String, String> {
    let segs = crate::model::pointer_segments(w.base_value(), &op.path, false)?;
    let mut s = String::new();
    for seg in segs {
        match seg {
            Seg::Key(k) => {
                if !s.is_empty() {
                    s.push('.');
                }
                s.push_str(&escape(&k));
            }
            Seg::Index(i) => s.push_str(&index(i)),
        }
    }
    Ok(s)
}
