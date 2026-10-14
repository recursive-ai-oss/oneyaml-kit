//! serde_yaml_ng: serde binding and `Value`. No lossless document.

use super::key_text;
use crate::model::*;
use crate::typed::{typed_runner, TypedParser};
use serde::de::{Deserialize, DeserializeOwned};
use serde_json::Value;
use serde_yaml_ng::Value as YValue;

pub struct SerdeYamlNg;

#[derive(Clone, Copy)]
struct Typed;

impl TypedParser for Typed {
    fn parse<T: DeserializeOwned>(&self, src: &'static str) -> Result<T, String> {
        serde_yaml_ng::from_str::<T>(src).map_err(|e| e.to_string())
    }
}

fn to_json(v: &YValue) -> Result<Value, String> {
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
            for (k, v) in m {
                out.insert(key_text(&to_json(k)?), to_json(v)?);
            }
            Value::Object(out)
        }
        YValue::Tagged(t) => to_json(&t.value)?,
    })
}

fn load_all(src: &str, single: bool) -> Result<Vec<YValue>, String> {
    if single {
        return serde_yaml_ng::from_str::<YValue>(src)
            .map(|v| vec![v])
            .map_err(|e| e.to_string());
    }
    serde_yaml_ng::Deserializer::from_str(src)
        .map(|d| YValue::deserialize(d).map_err(|e| e.to_string()))
        .collect()
}

impl Library for SerdeYamlNg {
    fn id(&self) -> &'static str {
        "serde_yaml_ng"
    }
    fn package(&self) -> (&'static str, &'static str) {
        ("serde_yaml_ng", env!("LOCKED_SERDE_YAML_NG"))
    }
    fn role(&self) -> Role {
        Role::Competitor
    }
    fn runner(&self, task: Task, w: &'static Workload) -> Result<Box<dyn Runner>, Unsupported> {
        let src = w.input_str;
        let single = w.documents == 1;
        Ok(match task {
            Task::TypedLoad => {
                let model = w
                    .typed_model
                    .as_deref()
                    .ok_or(Unsupported("no typed model for this workload"))?;
                typed_runner(Typed, model, src).ok_or(Unsupported("unknown typed model"))?
            }
            Task::GenericLoad => runner(
                || (),
                move |()| load_all(src, single),
                |docs| match docs.iter().map(to_json).collect::<Result<Vec<_>, _>>() {
                    Ok(d) => Product::Value(docs_value(d)),
                    Err(e) => Product::Value(Value::String(e)),
                },
            ),
            Task::Dump => {
                if w.documents > 1 {
                    return Err(Unsupported("dump is defined for one document"));
                }
                runner(
                    move || load_all(src, single),
                    |t| match t {
                        Ok(docs) => docs
                            .first()
                            .map(|v| serde_yaml_ng::to_string(v).map_err(|e| e.to_string()))
                            .unwrap_or(Err("no document".into())),
                        Err(e) => Err(e.clone()),
                    },
                    |s| Product::Bytes(s.into_bytes()),
                )
            }
            Task::DocumentParse | Task::UnchangedEmit | Task::EditScript => {
                return Err(Unsupported(
                    "no lossless document: comments and layout are discarded",
                ))
            }
            Task::Patch => return Err(Unsupported("no patch engine")),
        })
    }
}
