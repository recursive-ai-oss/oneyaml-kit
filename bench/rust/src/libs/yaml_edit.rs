//! yaml-edit: a lossless syntax tree with in-place edits. No data model, so the guard
//! judges its document on the bytes it writes back.

use super::dotted_path;
use crate::model::*;
use serde_json::Value;
use yaml_edit::path::YamlPath;
use yaml_edit::YamlFile;

pub struct YamlEdit;

fn parse(src: &str) -> Result<YamlFile, String> {
    let p = YamlFile::parse(src);
    if let Some(e) = p.errors().first() {
        return Err(e.to_string());
    }
    Ok(p.tree())
}

fn path_escape(k: &str) -> String {
    k.replace('\\', "\\\\")
        .replace('.', "\\.")
        .replace('[', "\\[")
}

fn set(doc: &yaml_edit::Document, path: &str, v: &Value) -> Result<(), String> {
    let r = match v {
        Value::String(s) => doc.try_set_path(path, s.as_str()),
        Value::Bool(b) => doc.try_set_path(path, *b),
        Value::Number(n) => match n.as_i64() {
            Some(i) => doc.try_set_path(path, i),
            None => doc.try_set_path(path, n.as_f64().unwrap_or(0.0)),
        },
        _ => return Err("unsupported value kind".into()),
    };
    r.map_err(|e| e.to_string())
}

fn apply(file: &YamlFile, w: &Workload) -> Result<(), String> {
    let doc = file.document().ok_or("no document")?;
    for op in &w.edit_ops {
        let path = dotted_path(w, op, path_escape, |i| format!(".{i}"))?;
        match op.op.as_str() {
            "replace" => set(
                &doc,
                &path,
                op.value.as_ref().ok_or("replace without a value")?,
            )?,
            "remove" => {
                doc.try_remove_path(&path).map_err(|e| e.to_string())?;
            }
            other => return Err(format!("unsupported edit {other}")),
        }
    }
    Ok(())
}

impl Library for YamlEdit {
    fn id(&self) -> &'static str {
        "yaml-edit"
    }
    fn package(&self) -> (&'static str, &'static str) {
        ("yaml-edit", env!("LOCKED_YAML_EDIT"))
    }
    fn role(&self) -> Role {
        Role::Competitor
    }
    fn runner(&self, task: Task, w: &'static Workload) -> Result<Box<dyn Runner>, Unsupported> {
        let src = w.input_str;
        Ok(match task {
            Task::TypedLoad => return Err(Unsupported("no data binding")),
            Task::GenericLoad => return Err(Unsupported("no data model: it is a syntax tree")),
            Task::Dump => return Err(Unsupported("no data model to dump")),
            Task::DocumentParse => runner(
                || (),
                move |()| parse(src),
                |f| Product::Document {
                    value: None,
                    emitted: f.to_string().into_bytes(),
                },
            ),
            Task::UnchangedEmit => runner(
                move || parse(src),
                |f| f.map(|f| f.to_string().into_bytes()),
                Product::Bytes,
            ),
            Task::EditScript => {
                if w.documents > 1 || w.edit_ops.is_empty() {
                    return Err(Unsupported("no edit script for this workload"));
                }
                runner(
                    move || parse(src),
                    move |f| {
                        let f = f?;
                        apply(&f, w)?;
                        Ok::<_, String>(f.to_string().into_bytes())
                    },
                    Product::Bytes,
                )
            }
            Task::Patch => return Err(Unsupported("no RFC 6902 patch engine")),
        })
    }
}
