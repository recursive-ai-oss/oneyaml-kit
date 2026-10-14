//! saphyr: a YAML 1.2 parser with a generic `Yaml` tree and an emitter. No serde binding
//! (that is a separate crate) and no lossless document.

use super::key_text;
use crate::model::*;
use saphyr::{LoadableYamlNode, Scalar, Yaml, YamlEmitter};
use saphyr_parser::Parser;
use serde_json::Value;

pub struct Saphyr;

fn to_json(v: &Yaml) -> Result<Value, String> {
    Ok(match v {
        Yaml::Value(s) => match s {
            Scalar::Null => Value::Null,
            Scalar::Boolean(b) => Value::Bool(*b),
            Scalar::Integer(i) => Value::from(*i),
            Scalar::FloatingPoint(f) => serde_json::Number::from_f64(f.0)
                .map(Value::Number)
                .ok_or("non-finite float")?,
            Scalar::String(s) => Value::String(s.to_string()),
        },
        Yaml::Representation(text, _, _) => Value::String(text.to_string()),
        Yaml::Sequence(s) => Value::Array(s.iter().map(to_json).collect::<Result<_, _>>()?),
        Yaml::Mapping(m) => {
            let mut out = serde_json::Map::new();
            for (k, v) in m {
                out.insert(key_text(&to_json(k)?), to_json(v)?);
            }
            Value::Object(out)
        }
        Yaml::Tagged(_, inner) => to_json(inner)?,
        Yaml::Alias(n) => return Err(format!("unresolved alias {n}")),
        Yaml::BadValue => return Err("bad value".into()),
    })
}

/// Loads every document through the parser's string input, the fast path of the crate.
fn load_all(src: &str) -> Result<Vec<Yaml<'_>>, String> {
    let mut parser = Parser::new_from_str(src);
    Yaml::load_from_parser(&mut parser).map_err(|e| e.to_string())
}

pub fn generic_json(bytes: &[u8]) -> Result<Value, String> {
    let s = std::str::from_utf8(bytes).map_err(|e| e.to_string())?;
    let docs = load_all(s)?
        .iter()
        .map(to_json)
        .collect::<Result<Vec<_>, _>>()?;
    Ok(docs_value(docs))
}

impl Library for Saphyr {
    fn id(&self) -> &'static str {
        "saphyr"
    }
    fn package(&self) -> (&'static str, &'static str) {
        ("saphyr", env!("LOCKED_SAPHYR"))
    }
    fn role(&self) -> Role {
        Role::Competitor
    }
    fn settings(&self) -> std::collections::BTreeMap<&'static str, String> {
        [(
            "entry point",
            "Parser::new_from_str + Yaml::load_from_parser".to_string(),
        )]
        .into_iter()
        .collect()
    }
    fn runner(&self, task: Task, w: &'static Workload) -> Result<Box<dyn Runner>, Unsupported> {
        let src = w.input_str;
        Ok(match task {
            Task::TypedLoad => return Err(Unsupported("no serde binding in this crate")),
            Task::GenericLoad => runner(
                || (),
                move |()| load_all(src),
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
                    move || load_all(src),
                    |t| {
                        let docs = t?;
                        let doc = docs.first().ok_or("no document")?;
                        let mut out = String::new();
                        YamlEmitter::new(&mut out)
                            .dump(doc)
                            .map_err(|e| e.to_string())?;
                        Ok::<_, String>(out)
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
