//! yaml-edit: a lossless syntax tree with in-place edits. No data model, so the guard
//! judges its document on the bytes it writes back.

use super::{failing_runner, resolve_steps, Step};
use crate::model::*;
use serde_json::Value;
use yaml_edit::path::YamlPath;
use yaml_edit::{Document, Mapping, Sequence, YamlFile, YamlNode};

pub struct YamlEdit;

/// A parsed document owns all of its text.
impl Held for YamlFile {
    fn held(&self, _: &mut Vec<(usize, usize)>) {}
}

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

/// Calls `$body` with `$x` bound to the JSON scalar as a value yaml-edit accepts.
macro_rules! with_value {
    ($v:expr, |$x:ident| $body:expr) => {
        match $v {
            Value::String(s) => {
                let $x = s.as_str();
                $body
            }
            Value::Bool(b) => {
                let $x = *b;
                $body
            }
            Value::Number(n) => match n.as_i64() {
                Some(i) => {
                    let $x = i;
                    $body
                }
                None => {
                    let $x = n.as_f64().unwrap_or(0.0);
                    $body
                }
            },
            _ => Err("unsupported value kind".to_string()),
        }
    };
}

/// A collection yaml-edit can edit in place.
enum Node {
    Map(Mapping),
    Seq(Sequence),
}

fn collection(n: YamlNode) -> Option<Node> {
    match n {
        YamlNode::Mapping(m) => Some(Node::Map(m)),
        YamlNode::Sequence(s) => Some(Node::Seq(s)),
        _ => None,
    }
}

/// The path API addresses a mapping key under a mapping root. Removing a sequence item, or
/// editing a document whose root is a sequence, is outside it, so those edits go through
/// the node API (`Sequence::set`, `Sequence::remove`, `Document::as_sequence`).
fn needs_nodes(root_is_mapping: bool, step: &Step) -> bool {
    !root_is_mapping || matches!(step.segs.last(), Some(Seg::Index(_)))
}

fn node_edit(doc: &Document, step: &Step) -> Result<(), String> {
    let root = doc
        .as_mapping()
        .map(Node::Map)
        .or_else(|| doc.as_sequence().map(Node::Seq))
        .ok_or("the document root is not a collection")?;
    let (last, parents) = step.segs.split_last().ok_or("empty path")?;
    let mut cur = root;
    for seg in parents {
        let next = match (&cur, seg) {
            (Node::Map(m), Seg::Key(k)) => m.get(k.as_str()),
            (Node::Seq(s), Seg::Index(i)) => s.get(*i),
            _ => None,
        };
        cur = next
            .and_then(collection)
            .ok_or_else(|| format!("no collection on the way to {}", step.dotted))?;
    }
    let missing = || format!("nothing at {}", step.dotted);
    match (step.op.as_str(), &cur, last) {
        ("replace", Node::Map(m), Seg::Key(k)) => {
            let v = step.value.as_ref().ok_or("replace without a value")?;
            with_value!(v, |x| {
                m.set(k.as_str(), x);
                Ok(())
            })
        }
        ("replace", Node::Seq(s), Seg::Index(i)) => {
            let v = step.value.as_ref().ok_or("replace without a value")?;
            with_value!(v, |x| if s.set(*i, x) { Ok(()) } else { Err(missing()) })
        }
        ("remove", Node::Map(m), Seg::Key(k)) => {
            m.remove(k.as_str()).map(|_| ()).ok_or_else(missing)
        }
        ("remove", Node::Seq(s), Seg::Index(i)) => s.remove(*i).map(|_| ()).ok_or_else(missing),
        (other, _, _) => Err(format!("unsupported edit {other} at {}", step.dotted)),
    }
}

fn path_edit(doc: &Document, step: &Step) -> Result<(), String> {
    match step.op.as_str() {
        "replace" => {
            let v = step.value.as_ref().ok_or("replace without a value")?;
            with_value!(v, |x| doc
                .try_set_path(&step.dotted, x)
                .map_err(|e| e.to_string()))
        }
        "remove" => doc
            .try_remove_path(&step.dotted)
            .map(|_| ())
            .map_err(|e| e.to_string()),
        other => Err(format!("unsupported edit {other}")),
    }
}

fn apply(file: &YamlFile, steps: &[Step], root_is_mapping: bool) -> Result<(), String> {
    let doc = file.document().ok_or("no document")?;
    for step in steps {
        if needs_nodes(root_is_mapping, step) {
            node_edit(&doc, step)?;
        } else {
            path_edit(&doc, step)?;
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
                move || OwnedInput::new(src),
                move |own| parse(own.as_str()),
                |f| Product::Document {
                    value: None,
                    emitted: f.to_string().into_bytes(),
                },
            ),
            Task::UnchangedEmit => runner(
                move || parse(OwnedInput::new(src).as_str()),
                |f| {
                    f.as_ref()
                        .map(|f| f.to_string().into_bytes())
                        .map_err(|e| e.clone())
                },
                Product::Bytes,
            ),
            Task::EditScript => {
                if w.documents > 1 || w.edit_ops.is_empty() {
                    return Err(Unsupported("no edit script for this workload"));
                }
                let steps = match resolve_steps(w, &w.edit_ops, path_escape, |i| format!(".{i}")) {
                    Ok(s) => s,
                    Err(e) => return Ok(failing_runner(e)),
                };
                let root_is_mapping = w.base_value().is_object();
                runner(
                    move || parse(OwnedInput::new(src).as_str()),
                    move |f| {
                        let f = f.as_ref().map_err(|e| e.clone())?;
                        apply(f, &steps, root_is_mapping)?;
                        Ok::<_, String>(f.to_string().into_bytes())
                    },
                    Product::Bytes,
                )
            }
            Task::Patch => return Err(Unsupported("no RFC 6902 patch engine")),
        })
    }
}
