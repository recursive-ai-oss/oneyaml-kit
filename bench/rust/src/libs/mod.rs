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

#[cfg(test)]
mod ownership_tests {
    //! Every prepared state owns its input. A library that keeps or lazily reads the text it is
    //! given would otherwise see one cache-hot buffer in all the states of a batch while a library
    //! that copies sees many cold ones.
    use super::*;
    use crate::model::{range_of, OwnedInput, Task};
    use serde_json::json;

    fn workload() -> &'static Workload {
        let mut text = String::from("# top\nname: demo\nitems:\n");
        for i in 0..60 {
            text.push_str(&format!("  - id: {i}\n    label: \"entry number {i}\"\n"));
        }
        let input_str: &'static str = Box::leak(text.into_boxed_str());
        let op = |name: &str, path: &str, value: Option<serde_json::Value>| Op {
            op: name.into(),
            path: path.into(),
            value,
        };
        Box::leak(Box::new(Workload {
            id: "owned".into(),
            input: input_str.as_bytes(),
            input_str,
            documents: 1,
            tasks: Task::ALL.to_vec(),
            typed_model: Some("records".into()),
            expected: vec![("spec".into(), json!({"name": "demo", "items": []}))],
            edit_ops: vec![op("replace", "/name", Some(json!("other")))],
            edit_expected: vec![],
            patch_ops: vec![],
            patch_failing_ops: vec![],
            patch_expected: vec![],
            meta: serde_json::Value::Null,
        }))
    }

    /// The first problem with the ranges of a batch of states: a range inside the workload's own
    /// text, or two states with overlapping ranges.
    fn overlap(held: &[Vec<(usize, usize)>], master: (usize, usize)) -> Option<String> {
        let hit = |a: (usize, usize), b: (usize, usize)| a.0 < b.1 && b.0 < a.1;
        for (i, a) in held.iter().enumerate() {
            for &x in a {
                if hit(x, master) {
                    return Some(format!("state {i} reads the workload's own input"));
                }
                for (j, b) in held.iter().enumerate().skip(i + 1) {
                    if b.iter().any(|&y| hit(x, y)) {
                        return Some(format!("states {i} and {j} share an input buffer"));
                    }
                }
            }
        }
        None
    }

    #[test]
    fn no_two_prepared_states_share_an_input() {
        let w = workload();
        let master = range_of(w.input_str);
        let mut checked = 0;
        // The controls hand back canned answers and read no input.
        for lib in all()
            .into_iter()
            .filter(|l| l.role() != crate::model::Role::Control)
        {
            for task in Task::ALL {
                let Ok(r) = lib.runner(task, w) else { continue };
                let held = r.prepare(4).held_inputs();
                assert_eq!(held.len(), 4);
                if let Some(why) = overlap(&held, master) {
                    panic!("{} {}: {why}", lib.id(), task.id());
                }
                // These tasks read the input inside the clock, so each state holds a whole copy of it.
                if matches!(
                    task,
                    Task::TypedLoad | Task::GenericLoad | Task::DocumentParse
                ) {
                    for (i, h) in held.iter().enumerate() {
                        assert!(
                            h.iter().any(|&(lo, hi)| hi - lo == w.input_str.len()),
                            "{} {}: state {i} holds no copy of the input it reads",
                            lib.id(),
                            task.id()
                        );
                    }
                }
                checked += 1;
            }
        }
        assert!(
            checked >= 10,
            "only {checked} library and task pairs were checked"
        );
    }

    #[test]
    fn the_overlap_check_finds_shared_and_master_ranges() {
        let a = OwnedInput::new(&"x".repeat(100));
        let b = OwnedInput::new(&"x".repeat(100));
        let range = |i: &OwnedInput| range_of(i.as_str());
        let master = (0, 0);
        assert!(overlap(&[vec![range(&a)], vec![range(&b)], vec![]], master).is_none());
        assert!(overlap(&[vec![range(&a)], vec![range(&a)]], master).is_some());
        let r = range(&a);
        assert!(overlap(&[vec![(r.0 + 50, r.1 + 50)], vec![r]], master).is_some());
        assert!(overlap(&[vec![range(&b)]], r).is_none());
        assert!(overlap(&[vec![range(&a)]], r).is_some());
    }
}
