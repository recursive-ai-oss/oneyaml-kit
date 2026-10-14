//! The correctness guard: judges what a library produced before anything is timed.
//!
//! Comparison is complete: the whole value, every output byte. A library that fails the
//! guard gets a verdict and a reason in the results and no timing numbers.

use crate::model::{FailedPatch, Product, Task, Workload};
use serde_json::Value;

#[derive(Debug, PartialEq)]
pub enum Verdict {
    /// Correct. `variant` names the accepted expectation ("spec", or e.g. "merged").
    Ok { variant: String },
    /// Produced an answer that is not the expected one.
    Differs(String),
    /// Refused an input the specification accepts.
    Rejects(String),
}

impl Verdict {
    pub fn status(&self) -> &'static str {
        match self {
            Verdict::Ok { .. } => "ok",
            Verdict::Differs(_) => "differs",
            Verdict::Rejects(_) => "rejects",
        }
    }
}

fn clip(s: &str) -> String {
    let mut t: String = s.chars().take(80).collect();
    if s.chars().count() > 80 {
        t.push('…');
    }
    t
}

/// Where two values first differ, as a pointer and the two sides.
pub fn first_diff(got: &Value, want: &Value, at: &str) -> Option<String> {
    match (got, want) {
        (Value::Object(a), Value::Object(b)) => {
            for (k, bv) in b {
                let p = format!("{at}/{}", k.replace('~', "~0").replace('/', "~1"));
                match a.get(k) {
                    None => return Some(format!("missing {p}")),
                    Some(av) => {
                        if let Some(d) = first_diff(av, bv, &p) {
                            return Some(d);
                        }
                    }
                }
            }
            a.keys()
                .find(|k| !b.contains_key(*k))
                .map(|k| format!("unexpected {at}/{k}"))
        }
        (Value::Array(a), Value::Array(b)) => {
            for (i, (x, y)) in a.iter().zip(b).enumerate() {
                if let Some(d) = first_diff(x, y, &format!("{at}/{i}")) {
                    return Some(d);
                }
            }
            (a.len() != b.len()).then(|| format!("{at}: {} items, expected {}", a.len(), b.len()))
        }
        _ if got == want => None,
        _ => Some(format!(
            "at {}: got {}, expected {}",
            if at.is_empty() { "/" } else { at },
            clip(&got.to_string()),
            clip(&want.to_string())
        )),
    }
}

/// Where two byte strings first differ.
pub fn bytes_diff(got: &[u8], want: &[u8]) -> Option<String> {
    if got == want {
        return None;
    }
    let at = got
        .iter()
        .zip(want)
        .position(|(a, b)| a != b)
        .unwrap_or(got.len().min(want.len()));
    let ctx = |b: &[u8]| {
        let lo = at.saturating_sub(12);
        clip(&format!(
            "{:?}",
            String::from_utf8_lossy(&b[lo..(at + 24).min(b.len())])
        ))
    };
    Some(format!(
        "byte {at}: got {} bytes {}, expected {} bytes {}",
        got.len(),
        ctx(got),
        want.len(),
        ctx(want)
    ))
}

fn match_value(w: &Workload, got: &Value) -> Verdict {
    let mut first = None;
    for (name, want) in &w.expected {
        match first_diff(got, want, "") {
            None => {
                return Verdict::Ok {
                    variant: name.clone(),
                }
            }
            Some(d) if first.is_none() => first = Some(d),
            Some(_) => {}
        }
    }
    Verdict::Differs(first.unwrap_or_default())
}

fn match_bytes(got: &[u8], want: &[(String, &[u8])], what: &str) -> Verdict {
    let mut first = None;
    for (name, bytes) in want {
        match bytes_diff(got, bytes) {
            None => {
                return Verdict::Ok {
                    variant: name.clone(),
                }
            }
            Some(d) if first.is_none() => first = Some(d),
            Some(_) => {}
        }
    }
    Verdict::Differs(first.unwrap_or_else(|| format!("workload has no expected {what}")))
}

/// Judges one library's answer to one task.
///
/// `referee` parses bytes to the common form with a fixed pair of independent parsers; a
/// dumped document is accepted only if both read it back as the expected value.
pub fn judge(
    task: Task,
    w: &Workload,
    answer: Result<Product, String>,
    failing: Option<FailedPatch>,
    referee: &dyn Fn(&[u8]) -> Result<Value, String>,
) -> Verdict {
    let product = match answer {
        Ok(p) => p,
        Err(e) => return Verdict::Rejects(clip(&e)),
    };
    let v = match (task, product) {
        (Task::TypedLoad | Task::GenericLoad, Product::Value(v)) => match_value(w, &v),
        (Task::Dump, Product::Bytes(b)) => match referee(&b) {
            Err(e) => Verdict::Differs(format!("the output does not parse: {}", clip(&e))),
            Ok(v) => match first_diff(&v, w.base_value(), "") {
                None => Verdict::Ok {
                    variant: "spec".into(),
                },
                Some(d) => Verdict::Differs(format!("read back: {d}")),
            },
        },
        (Task::DocumentParse, Product::Document { value, emitted }) => {
            // A lossless document must hold the value and write the input back unchanged.
            let by_value = match &value {
                Some(v) => match_value(w, v),
                None => Verdict::Ok {
                    variant: "bytes-only".into(),
                },
            };
            match (by_value, bytes_diff(&emitted, w.input)) {
                (v @ Verdict::Ok { .. }, None) => v,
                (Verdict::Ok { .. }, Some(d)) => {
                    Verdict::Differs(format!("does not write the input back: {d}"))
                }
                (v, _) => v,
            }
        }
        (Task::UnchangedEmit, Product::Bytes(b)) => match bytes_diff(&b, w.input) {
            None => Verdict::Ok {
                variant: "spec".into(),
            },
            Some(d) => Verdict::Differs(d),
        },
        (Task::EditScript, Product::Bytes(b)) => match_bytes(&b, &w.edit_expected, "edit output"),
        (Task::Patch, Product::Bytes(b)) => {
            let ok = match_bytes(&b, &w.patch_expected, "patch output");
            match (&ok, failing) {
                (Verdict::Ok { .. }, Some(f)) if !f.errored => {
                    Verdict::Differs("a patch with a failing step reported success".into())
                }
                (Verdict::Ok { .. }, Some(f)) if f.bytes != w.input => Verdict::Differs(format!(
                    "a failing patch changed the document: {}",
                    bytes_diff(&f.bytes, w.input).unwrap_or_default()
                )),
                (Verdict::Ok { .. }, None) => {
                    Verdict::Differs("the failing patch was not checked".into())
                }
                _ => ok,
            }
        }
        (t, _) => Verdict::Differs(format!(
            "the runner for {} returned the wrong kind of product",
            t.id()
        )),
    };
    v
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn workload(expected: Vec<(&str, Value)>, input: &'static [u8]) -> Workload {
        Workload {
            id: "t".into(),
            input,
            input_str: "",
            documents: 1,
            tasks: vec![],
            typed_model: None,
            expected: expected
                .into_iter()
                .map(|(n, v)| (n.to_string(), v))
                .collect(),
            edit_ops: vec![],
            edit_expected: vec![("spec".into(), b"a: 2\n")],
            patch_ops: vec![],
            patch_failing_ops: vec![],
            patch_expected: vec![("spec".into(), b"a: 2\n"), ("restyled".into(), b"a: '2'\n")],
            meta: Value::Null,
        }
    }

    fn no_referee(_: &[u8]) -> Result<Value, String> {
        Err("unused".into())
    }

    #[test]
    fn equal_values_pass_and_any_difference_fails() {
        let w = workload(vec![("spec", json!({"a": [1, 2.5, null, "x"]}))], b"a: 1\n");
        let ok = judge(
            Task::GenericLoad,
            &w,
            Ok(Product::Value(json!({"a": [1, 2.5, null, "x"]}))),
            None,
            &no_referee,
        );
        assert_eq!(
            ok,
            Verdict::Ok {
                variant: "spec".into()
            }
        );
        // An integer is not the float with the same digits, and "1" is not 1.
        for bad in [
            json!({"a": [1.0, 2.5, null, "x"]}),
            json!({"a": ["1", 2.5, null, "x"]}),
            json!({"a": [1, 2.5, null]}),
            json!({"a": [1, 2.5, null, "x"], "b": 1}),
        ] {
            let v = judge(
                Task::GenericLoad,
                &w,
                Ok(Product::Value(bad)),
                None,
                &no_referee,
            );
            assert_eq!(v.status(), "differs", "{v:?}");
        }
    }

    #[test]
    fn a_second_expectation_is_named_when_it_matches() {
        let w = workload(
            vec![("spec", json!({"<<": 1})), ("merged", json!({"a": 1}))],
            b"",
        );
        let v = judge(
            Task::GenericLoad,
            &w,
            Ok(Product::Value(json!({"a": 1}))),
            None,
            &no_referee,
        );
        assert_eq!(
            v,
            Verdict::Ok {
                variant: "merged".into()
            }
        );
    }

    #[test]
    fn rejection_is_its_own_verdict() {
        let w = workload(vec![("spec", json!(1))], b"");
        let v = judge(Task::GenericLoad, &w, Err("bad".into()), None, &no_referee);
        assert_eq!(v, Verdict::Rejects("bad".into()));
    }

    #[test]
    fn bytes_must_match_exactly() {
        let w = workload(vec![("spec", json!(1))], b"a: 1\n");
        let same = judge(
            Task::UnchangedEmit,
            &w,
            Ok(Product::Bytes(b"a: 1\n".to_vec())),
            None,
            &no_referee,
        );
        assert_eq!(same.status(), "ok");
        for bad in [&b"a: 1"[..], b"a:  1\n", b"a: 1\r\n", b"\xef\xbb\xbfa: 1\n"] {
            let v = judge(
                Task::UnchangedEmit,
                &w,
                Ok(Product::Bytes(bad.to_vec())),
                None,
                &no_referee,
            );
            assert_eq!(v.status(), "differs");
        }
        let e = judge(
            Task::EditScript,
            &w,
            Ok(Product::Bytes(b"a: 2\n".to_vec())),
            None,
            &no_referee,
        );
        assert_eq!(e.status(), "ok");
        let e = judge(
            Task::EditScript,
            &w,
            Ok(Product::Bytes(b"a: 3\n".to_vec())),
            None,
            &no_referee,
        );
        assert_eq!(e.status(), "differs");
    }

    #[test]
    fn dump_is_judged_by_reading_it_back() {
        let w = workload(vec![("spec", json!({"a": 1}))], b"");
        let good = |_: &[u8]| Ok(json!({"a": 1}));
        let wrong = |_: &[u8]| Ok(json!({"a": "1"}));
        let v = judge(
            Task::Dump,
            &w,
            Ok(Product::Bytes(b"anything".to_vec())),
            None,
            &good,
        );
        assert_eq!(v.status(), "ok");
        let v = judge(
            Task::Dump,
            &w,
            Ok(Product::Bytes(b"anything".to_vec())),
            None,
            &wrong,
        );
        assert_eq!(v.status(), "differs");
        let v = judge(
            Task::Dump,
            &w,
            Ok(Product::Bytes(b"anything".to_vec())),
            None,
            &no_referee,
        );
        assert_eq!(v.status(), "differs");
    }

    #[test]
    fn a_document_must_also_write_the_input_back() {
        let w = workload(vec![("spec", json!({"a": 1}))], b"a: 1\n");
        let mk = |e: &[u8]| Product::Document {
            value: Some(json!({"a": 1})),
            emitted: e.to_vec(),
        };
        assert_eq!(
            judge(
                Task::DocumentParse,
                &w,
                Ok(mk(b"a: 1\n")),
                None,
                &no_referee
            )
            .status(),
            "ok"
        );
        assert_eq!(
            judge(
                Task::DocumentParse,
                &w,
                Ok(mk(b"a:  1\n")),
                None,
                &no_referee
            )
            .status(),
            "differs"
        );
    }

    #[test]
    fn an_atomic_patch_must_leave_the_document_alone() {
        let w = workload(vec![("spec", json!(1))], b"a: 1\n");
        let run = |f: Option<FailedPatch>| {
            judge(
                Task::Patch,
                &w,
                Ok(Product::Bytes(b"a: 2\n".to_vec())),
                f,
                &no_referee,
            )
        };
        assert_eq!(
            run(Some(FailedPatch {
                errored: true,
                bytes: b"a: 1\n".to_vec()
            }))
            .status(),
            "ok"
        );
        assert_eq!(
            run(Some(FailedPatch {
                errored: false,
                bytes: b"a: 1\n".to_vec()
            }))
            .status(),
            "differs"
        );
        assert_eq!(
            run(Some(FailedPatch {
                errored: true,
                bytes: b"a: 2\n".to_vec()
            }))
            .status(),
            "differs"
        );
        assert_eq!(run(None).status(), "differs");
    }

    #[test]
    fn diff_names_the_first_difference() {
        let d = first_diff(&json!({"a": {"b": 1}}), &json!({"a": {"b": 2}}), "").unwrap();
        assert!(d.contains("/a/b"), "{d}");
    }
}
