//! oneyaml-rust: serde binding, `Value`, and the lossless `Document`.

use super::{failing_runner, key_text, resolve_steps, Step};
use crate::model::*;
use crate::typed::{typed_runner, TypedParser};
use oneyaml::{Document, Stream, Value as YValue};
use serde::de::{Deserialize, DeserializeOwned};
use serde_json::Value;

pub struct OneYaml;

/// A parsed document owns all of its text.
impl Held for Document {
    fn held(&self, _: &mut Vec<(usize, usize)>) {}
}

/// A parsed document owns all of its text.
impl Held for Stream {
    fn held(&self, _: &mut Vec<(usize, usize)>) {}
}

/// A parsed document owns all of its text.
impl Held for Vec<YValue> {
    fn held(&self, _: &mut Vec<(usize, usize)>) {}
}

#[derive(Clone, Copy)]
struct Typed;

impl TypedParser for Typed {
    fn parse<T: DeserializeOwned>(&self, src: &str) -> Result<T, String> {
        oneyaml::from_str::<T>(src).map_err(|e| e.to_string())
    }
}

/// The common form of a oneyaml value.
pub fn to_json(v: &YValue) -> Result<Value, String> {
    Ok(match v {
        YValue::Null => Value::Null,
        YValue::Bool(b) => Value::Bool(*b),
        YValue::Number(n) => {
            if let Some(i) = n.as_i64() {
                Value::from(i)
            } else if let Some(u) = n.as_u64() {
                Value::from(u)
            } else {
                let f = n.as_f64().ok_or("number out of range")?;
                serde_json::Number::from_f64(f)
                    .map(Value::Number)
                    .ok_or("non-finite float")?
            }
        }
        YValue::String(s) => Value::String(s.clone()),
        YValue::Sequence(s) => Value::Array(s.iter().map(to_json).collect::<Result<_, _>>()?),
        YValue::Mapping(m) => {
            let mut out = serde_json::Map::new();
            for (k, v) in m.iter() {
                out.insert(key_text(&to_json(k)?), to_json(v)?);
            }
            Value::Object(out)
        }
        YValue::Tagged(t) => to_json(&t.clone().into_parts().1)?,
        _ => return Err("unknown value kind".into()),
    })
}

/// All documents of a text as values.
fn load_all(src: &str, single: bool) -> Result<Vec<YValue>, String> {
    if single {
        // The entry point a caller with one document uses.
        return oneyaml::from_str::<YValue>(src)
            .map(|v| vec![v])
            .map_err(|e| e.to_string());
    }
    oneyaml::Deserializer::from_str(src)
        .map(|d| YValue::deserialize(d).map_err(|e| e.to_string()))
        .collect()
}

/// Reads text to the common form with oneyaml (also the referee's reader).
pub fn generic_json(bytes: &[u8]) -> Result<Value, String> {
    let s = std::str::from_utf8(bytes).map_err(|e| e.to_string())?;
    let docs = load_all(s, false)?
        .iter()
        .map(to_json)
        .collect::<Result<Vec<_>, _>>()?;
    Ok(docs_value(docs))
}

fn path_escape(k: &str) -> String {
    let plain = !k.is_empty()
        && !k.chars().any(|c| {
            matches!(c, '.' | '[' | ']' | '"' | '\\') || c.is_whitespace() || c.is_control()
        });
    if plain {
        k.to_string()
    } else {
        format!("\"{}\"", k.replace('\\', "\\\\").replace('"', "\\\""))
    }
}

fn parse_doc(w: &Workload) -> Result<Document, String> {
    Document::parse(OwnedInput::new(w.input_str).as_str()).map_err(|e| e.to_string())
}

fn apply(doc: &mut Document, steps: &[Step]) -> Result<(), String> {
    for step in steps {
        match step.op.as_str() {
            "replace" => {
                let v = step.value.as_ref().ok_or("replace without a value")?;
                doc.set_path(step.dotted.as_str(), v)
                    .map_err(|e| e.to_string())?;
            }
            "remove" => {
                let _removed = doc
                    .remove_path(step.dotted.as_str())
                    .map_err(|e| e.to_string())?;
            }
            other => return Err(format!("unsupported edit {other}")),
        }
    }
    Ok(())
}

impl Library for OneYaml {
    fn id(&self) -> &'static str {
        "oneyaml"
    }
    fn package(&self) -> (&'static str, &'static str) {
        ("oneyaml", env!("LOCKED_ONEYAML"))
    }
    fn role(&self) -> Role {
        Role::Own
    }
    fn settings(&self) -> std::collections::BTreeMap<&'static str, String> {
        [
            ("features", "default-features = false".to_string()),
            ("schema", "core (YAML 1.2)".to_string()),
        ]
        .into_iter()
        .collect()
    }

    fn runner(&self, task: Task, w: &'static Workload) -> Result<Box<dyn Runner>, Unsupported> {
        let src = w.input_str;
        let multi = w.documents > 1;
        let single = !multi;
        Ok(match task {
            Task::TypedLoad => {
                let model = w
                    .typed_model
                    .as_deref()
                    .ok_or(Unsupported("no typed model for this workload"))?;
                typed_runner(Typed, model, src).ok_or(Unsupported("unknown typed model"))?
            }
            Task::GenericLoad => runner(
                move || OwnedInput::new(src),
                move |own| load_all(own.as_str(), single),
                |docs| match docs.iter().map(to_json).collect::<Result<Vec<_>, _>>() {
                    Ok(d) => Product::Value(docs_value(d)),
                    Err(e) => Product::Value(Value::String(e)),
                },
            ),
            Task::Dump => {
                if multi {
                    return Err(Unsupported("dump is defined for one document"));
                }
                runner(
                    move || load_all(OwnedInput::new(src).as_str(), single),
                    |t| match t {
                        Ok(docs) => docs
                            .first()
                            .map(|v| oneyaml::to_string(v).map_err(|e| e.to_string()))
                            .unwrap_or(Err("no document".into())),
                        Err(e) => Err(e.clone()),
                    },
                    |s| Product::Bytes(s.into_bytes()),
                )
            }
            Task::DocumentParse => {
                if multi {
                    runner(
                        move || OwnedInput::new(src),
                        move |own| Stream::parse(own.as_str()).map_err(|e| e.to_string()),
                        |s| {
                            let docs: Result<Vec<_>, _> = s
                                .documents()
                                .map(|d| {
                                    d.to_value()
                                        .map_err(|e| e.to_string())
                                        .and_then(|v| to_json(&v))
                                })
                                .collect();
                            match docs {
                                Ok(d) => Product::Document {
                                    value: Some(docs_value(d)),
                                    emitted: s.to_bytes(),
                                },
                                Err(e) => Product::Document {
                                    value: Some(Value::String(e)),
                                    emitted: vec![],
                                },
                            }
                        },
                    )
                } else {
                    runner(
                        move || OwnedInput::new(src),
                        move |own| Document::parse(own.as_str()).map_err(|e| e.to_string()),
                        |d| match d
                            .to_value()
                            .map_err(|e| e.to_string())
                            .and_then(|v| to_json(&v))
                        {
                            Ok(v) => Product::Document {
                                value: Some(v),
                                emitted: d.to_bytes(),
                            },
                            Err(e) => Product::Document {
                                value: Some(Value::String(e)),
                                emitted: vec![],
                            },
                        },
                    )
                }
            }
            Task::UnchangedEmit => {
                if multi {
                    runner(
                        move || {
                            Stream::parse(OwnedInput::new(src).as_str()).map_err(|e| e.to_string())
                        },
                        |s| s.as_ref().map(|s| s.to_bytes()).map_err(|e| e.clone()),
                        Product::Bytes,
                    )
                } else {
                    runner(
                        move || parse_doc(w),
                        |d| d.as_ref().map(|d| d.to_bytes()).map_err(|e| e.clone()),
                        Product::Bytes,
                    )
                }
            }
            Task::EditScript => {
                if multi || w.edit_ops.is_empty() {
                    return Err(Unsupported("no edit script for this workload"));
                }
                let steps = match resolve_steps(w, &w.edit_ops, path_escape, |i| format!("[{i}]")) {
                    Ok(s) => s,
                    Err(e) => return Ok(failing_runner(e)),
                };
                runner(
                    move || parse_doc(w),
                    move |d| {
                        let d = d.as_mut().map_err(|e| e.clone())?;
                        apply(d, &steps)?;
                        Ok::<_, String>(d.to_bytes())
                    },
                    Product::Bytes,
                )
            }
            Task::Patch => return Err(Unsupported("no RFC 6902 patch engine in this release")),
        })
    }
}
