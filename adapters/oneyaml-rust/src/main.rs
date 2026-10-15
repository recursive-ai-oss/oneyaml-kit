//! Kit adapter for oneyaml-rust.
//!
//! Speaks the kit's JSON Lines protocol on stdin and stdout (one request per
//! line, one response per line). The library has no patch engine, so edits are
//! translated onto its document API for the operations that map directly
//! (`replace` and `remove`); everything else is declared unsupported.

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use oneyaml::{Document, Mapping, NodeKind, Path, Stream, Value};
use oneyaml_core::{ColumnUnit, Diagnostic, LineIndex};
use oneyaml_syntax::event::{CollectionStyle, EventKind, ScalarStyle};
use serde_json::Value as Json;
use std::io::{BufRead, Write};

const CAPABILITIES: &[&str] = &[
    "events",
    "value",
    "dump",
    "patch:replace",
    "patch:remove",
    "error-category",
    "error-position",
    "error-events",
];

/// A failure the kit understands: its category and, where known, a 1-based
/// line and column counted in characters.
struct Failure {
    category: &'static str,
    position: Option<(usize, usize)>,
    message: String,
    /// Behaviours this adapter supplied itself, not the library (see `EMULATES`).
    emulated: &'static [&'static str],
}

/// What the adapter does on the library's behalf. Declared in the hello and named in
/// every response that relied on it, so a report never credits the library for it.
///
/// - `unchanged`: the library applies one edit at a time and has no atomic patch, so
///   the adapter edits a working copy and reports the original bytes when any step fails.
/// - `error`: the error itself came from the adapter (a missing path, a malformed
///   pointer, an unknown operation name), not from the library.
const EMULATES: &[&str] = &["unchanged", "error"];

enum Outcome {
    Events(Vec<String>),
    Value(String),
    Output(Vec<u8>),
    Failed(Failure, Extra),
    Unsupported(String),
}

/// What an error response carries besides the error itself.
enum Extra {
    None,
    Events(Vec<String>),
    Output(Vec<u8>),
}

fn js(s: &str) -> String {
    serde_json::to_string(s).unwrap_or_else(|_| "\"\"".into())
}

fn category_of(d: &Diagnostic) -> &'static str {
    match d.code.get() {
        1..=999 => "syntax",
        2000..=2999 => "limit",
        _ => "other",
    }
}

fn position(text: &str, d: &Diagnostic) -> Option<(usize, usize)> {
    let lc = LineIndex::new(text).line_col(d.primary.span.start, ColumnUnit::Char)?;
    Some((lc.line + 1, lc.column + 1))
}

// ---- events -------------------------------------------------------------

fn escape(out: &mut String, value: &str) {
    for c in value.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\0' => out.push_str("\\0"),
            '\u{8}' => out.push_str("\\b"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            other => out.push(other),
        }
    }
}

fn events(bytes: &[u8]) -> Outcome {
    let decoded = match oneyaml_syntax::decode_strict(bytes) {
        Ok(d) => d,
        Err(e) => {
            let f = Failure {
                category: "encoding",
                position: None,
                message: e.0.message.clone(),
                emulated: &[],
            };
            return Outcome::Failed(f, Extra::Events(Vec::new()));
        }
    };
    let text = decoded.text();
    let list = oneyaml_syntax::parse(text);
    let mut lines: Vec<String> = Vec::new();
    for ev in list.sourced() {
        let mut l = String::new();
        let unreadable = || Outcome::Unsupported("event payload cannot be read".into());
        match ev.kind() {
            EventKind::Error(_) => {
                let (category, pos, message) = match ev.event().diagnostic() {
                    Some(d) => (category_of(d), position(text, d), d.message.clone()),
                    None => ("other", None, "error".to_string()),
                };
                return Outcome::Failed(
                    Failure {
                        category,
                        position: pos,
                        message,
                        emulated: &[],
                    },
                    Extra::Events(lines),
                );
            }
            EventKind::Trivia(_) | EventKind::Directive(_) | EventKind::Warning(_) => continue,
            EventKind::StreamStart => l.push_str("+STR"),
            EventKind::StreamEnd => l.push_str("-STR"),
            EventKind::DocumentStart { explicit, .. } => {
                l.push_str(if *explicit { "+DOC ---" } else { "+DOC" })
            }
            EventKind::DocumentEnd { explicit, .. } => {
                l.push_str(if *explicit { "-DOC ..." } else { "-DOC" })
            }
            EventKind::MappingEnd => l.push_str("-MAP"),
            EventKind::SequenceEnd => l.push_str("-SEQ"),
            EventKind::MappingStart { style, .. } | EventKind::SequenceStart { style, .. } => {
                let is_map = matches!(ev.kind(), EventKind::MappingStart { .. });
                l.push_str(if is_map { "+MAP" } else { "+SEQ" });
                match style {
                    CollectionStyle::Block => {}
                    CollectionStyle::Flow | CollectionStyle::FlowPair => {
                        l.push_str(if is_map { " {}" } else { " []" })
                    }
                    _ => return Outcome::Unsupported("unknown collection style".into()),
                }
                if props(&mut l, &ev).is_none() {
                    return unreadable();
                }
            }
            EventKind::Scalar { style, .. } => {
                l.push_str("=VAL");
                if props(&mut l, &ev).is_none() {
                    return unreadable();
                }
                l.push(' ');
                l.push(match style {
                    ScalarStyle::Plain => ':',
                    ScalarStyle::SingleQuoted => '\'',
                    ScalarStyle::DoubleQuoted => '"',
                    ScalarStyle::Literal => '|',
                    ScalarStyle::Folded => '>',
                    _ => return Outcome::Unsupported("unknown scalar style".into()),
                });
                match ev.value() {
                    Some(v) => escape(&mut l, &v),
                    None => return unreadable(),
                }
            }
            EventKind::Alias => {
                l.push_str("=ALI *");
                match ev.alias_name() {
                    Some(n) => l.push_str(n),
                    None => return unreadable(),
                }
            }
            _ => return Outcome::Unsupported("unknown event kind".into()),
        }
        lines.push(l);
    }
    Outcome::Events(lines)
}

fn props(
    out: &mut String,
    ev: &oneyaml_syntax::event::Sourced<'_, &oneyaml_syntax::event::Event<'_>>,
) -> Option<()> {
    let p = ev.properties()?;
    if p.anchor.is_some() {
        out.push_str(" &");
        out.push_str(ev.anchor()?.name);
    }
    if p.tag.is_some() {
        out.push_str(" <");
        out.push_str(&ev.tag()?.resolved);
        out.push('>');
    }
    Some(())
}

// ---- values -------------------------------------------------------------

fn error_failure(e: &oneyaml::Error) -> Failure {
    // The library has a code of its own for two equal keys in a mapping.
    let category = if e.code() == oneyaml_core::code::DUPLICATE_KEY {
        "duplicate-key"
    } else {
        match e.kind() {
            oneyaml::ErrorKind::Syntax => "syntax",
            oneyaml::ErrorKind::Limit => "limit",
            _ => "other",
        }
    };
    let position = e.location().map(|l| (l.line(), l.column()));
    Failure {
        category,
        position,
        message: e.diagnostic().message.clone(),
        emulated: &[],
    }
}

/// Writes `v` as kit value JSON. Integers keep their digits; floats always
/// carry a decimal point or exponent; mappings with only string keys are JSON
/// objects, any other mapping is `{"$map": [[key, value], ...]}`.
fn write_value(v: &Value, out: &mut String, depth: usize) -> Result<(), String> {
    if depth > 200_000 {
        return Err("value nesting too deep for the adapter".into());
    }
    match v {
        Value::Tagged(t) => {
            // A tag the schema does not resolve is kept, never dropped.
            out.push_str("{\"$tagged\":{\"tag\":");
            out.push_str(&js(&t.tag.to_string()));
            out.push_str(",\"value\":");
            write_value(&t.value, out, depth + 1)?;
            out.push_str("}}");
        }
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::String(s) => out.push_str(&js(s)),
        Value::Number(n) => {
            if n.is_float() {
                let f = n.as_float().unwrap_or(f64::NAN);
                if f.is_nan() {
                    out.push_str("{\"$float\":\"nan\"}");
                } else if f.is_infinite() {
                    out.push_str(if f > 0.0 {
                        "{\"$float\":\"inf\"}"
                    } else {
                        "{\"$float\":\"-inf\"}"
                    });
                } else {
                    out.push_str(&format!("{f:?}"));
                }
            } else if let Some(i) = n.as_i128() {
                out.push_str(&i.to_string());
            } else {
                let s = n.to_core_string();
                let digits = s.strip_prefix('-').unwrap_or(&s);
                if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
                    return Err("integer beyond 128 bits in a non-decimal form".into());
                }
                out.push_str(&s);
            }
        }
        Value::Sequence(s) => {
            out.push('[');
            for (i, x) in s.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(x, out, depth + 1)?;
            }
            out.push(']');
        }
        Value::Mapping(m) => {
            // Keys starting with `$` are reserved for the kit's escape forms, so a mapping
            // that has one is written in the `$map` form.
            let all_strings = m
                .iter()
                .all(|(k, _)| matches!(k, Value::String(s) if !s.starts_with('$')));
            if all_strings {
                out.push('{');
                for (i, (k, x)) in m.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    if let Value::String(ks) = k {
                        out.push_str(&js(ks));
                    }
                    out.push(':');
                    write_value(x, out, depth + 1)?;
                }
                out.push('}');
            } else {
                out.push_str("{\"$map\":[");
                for (i, (k, x)) in m.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    out.push('[');
                    write_value(k, out, depth + 1)?;
                    out.push(',');
                    write_value(x, out, depth + 1)?;
                    out.push(']');
                }
                out.push_str("]}");
            }
        }
        _ => return Err("value kind unknown to the adapter".into()),
    }
    Ok(())
}

fn value(bytes: &[u8]) -> Outcome {
    let stream = match Stream::from_slice(bytes) {
        Ok(s) => s,
        Err(e) => return Outcome::Failed(error_failure(&e), Extra::None),
    };
    let mut out = String::from("[");
    for i in 0..stream.len() {
        let Some(doc) = stream.document(i) else {
            continue;
        };
        let v = match doc.to_value() {
            Ok(v) => v,
            Err(e) => return Outcome::Failed(error_failure(&e), Extra::None),
        };
        if i > 0 {
            out.push(',');
        }
        if let Err(m) = write_value(&v, &mut out, 0) {
            return Outcome::Unsupported(m);
        }
    }
    out.push(']');
    Outcome::Value(out)
}

// ---- dump -----------------------------------------------------------------

/// The library's own value for a kit value: a JSON number with no point or exponent is an
/// integer, any other is a float; `{"$float": ..}` is a non-finite float and `{"$map": ..}` a
/// mapping with any keys.
fn native(j: &Json, depth: usize) -> Result<Value, String> {
    if depth > 1000 {
        return Err("value nesting too deep for the adapter".into());
    }
    Ok(match j {
        Json::Null => Value::Null,
        Json::Bool(b) => Value::Bool(*b),
        Json::Number(n) => {
            if let Some(i) = n.as_i64() {
                Value::from(i)
            } else if let Some(u) = n.as_u64() {
                Value::from(u)
            } else {
                Value::from(n.as_f64().ok_or("number beyond f64")?)
            }
        }
        Json::String(s) => Value::String(s.clone()),
        Json::Array(a) => {
            let mut v = Vec::with_capacity(a.len());
            for x in a {
                v.push(native(x, depth + 1)?);
            }
            Value::from(v)
        }
        Json::Object(o) => {
            if let Some(f) = o.get("$float").and_then(Json::as_str) {
                return Ok(Value::from(match f {
                    "inf" => f64::INFINITY,
                    "-inf" => f64::NEG_INFINITY,
                    _ => f64::NAN,
                }));
            }
            if let Some(tagged) = o.get("$tagged") {
                // serialize_bytes writes a list of integers and the value type has no byte string,
                // so a binary node is declined, not wrapped in a tag, text or a list by the adapter.
                let binary =
                    tagged.get("tag").and_then(Json::as_str) == Some("tag:yaml.org,2002:binary");
                return Err(if binary {
                    "a byte slice is written as a list of integers, and the value type has no byte string".into()
                } else {
                    "tagged value".into()
                });
            }
            let mut m = Mapping::new();
            if let Some(pairs) = o.get("$map").and_then(Json::as_array) {
                for p in pairs {
                    let kv = p
                        .as_array()
                        .filter(|a| a.len() == 2)
                        .ok_or("bad $map pair")?;
                    m.insert(native(&kv[0], depth + 1)?, native(&kv[1], depth + 1)?);
                }
            } else {
                for (k, x) in o {
                    m.insert(Value::String(k.clone()), native(x, depth + 1)?);
                }
            }
            Value::from(m)
        }
    })
}

/// Writes the value with the library's default settings. The text is returned as it is.
fn dump(args: &Json, big_integer: bool) -> Outcome {
    // The JSON reader turns an integer beyond 64 bits into a float, so the request text is
    // checked for one beforehand; such a value is declined, never written rounded.
    if big_integer {
        return Outcome::Unsupported("integer beyond 64 bits".into());
    }
    let Some(j) = args.get("value") else {
        return Outcome::Unsupported("no value".into());
    };
    let v = match native(j, 0) {
        Ok(v) => v,
        Err(m) => return Outcome::Unsupported(m),
    };
    match v.to_yaml_string() {
        Ok(text) => Outcome::Output(text.into_bytes()),
        Err(e) => Outcome::Failed(
            Failure {
                category: "other",
                position: None,
                message: e.to_string(),
                emulated: &[],
            },
            Extra::None,
        ),
    }
}

// ---- patch ----------------------------------------------------------------

/// A failed patch. `own` is true when the adapter produced the error itself.
fn fail(category: &'static str, message: impl Into<String>, original: &[u8], own: bool) -> Outcome {
    let emulated: &'static [&'static str] = if own {
        &["unchanged", "error"]
    } else {
        &["unchanged"]
    };
    Outcome::Failed(
        Failure {
            category,
            position: None,
            message: message.into(),
            emulated,
        },
        Extra::Output(original.to_vec()),
    )
}

fn pointer_tokens(p: &str) -> Option<Vec<String>> {
    if p.is_empty() {
        return Some(Vec::new());
    }
    let rest = p.strip_prefix('/')?;
    Some(
        rest.split('/')
            .map(|t| t.replace("~1", "/").replace("~0", "~"))
            .collect(),
    )
}

/// Turns pointer tokens into the library's path by looking at what each step
/// lands on: an index into a sequence, a key into a mapping. Aliases are
/// followed for that decision only; whether the library then writes through
/// them is the library's own behaviour.
fn build_path(doc: &Document, tokens: &[String]) -> Result<Path, &'static str> {
    let mut path = Path::default();
    for t in tokens {
        let node = if path.is_empty() {
            doc.root()
        } else {
            doc.get_path(path.clone())
        };
        let mut node = node.ok_or("patch-path")?;
        if node.kind() == NodeKind::Alias {
            node = node.alias_target().ok_or("patch-path")?;
        }
        match node.kind() {
            NodeKind::Sequence => {
                let ok = !t.is_empty()
                    && t.bytes().all(|b| b.is_ascii_digit())
                    && (t == "0" || !t.starts_with('0'));
                if !ok {
                    return Err("patch-path");
                }
                path.push_index(t.parse::<usize>().map_err(|_| "patch-path")?);
            }
            NodeKind::Mapping => path.push_key(t.clone()),
            _ => return Err("patch-path"),
        }
    }
    Ok(path)
}

fn patch(bytes: &[u8], args: &Json) -> Outcome {
    if args
        .get("format")
        .and_then(Json::as_str)
        .unwrap_or("json-patch")
        != "json-patch"
    {
        return Outcome::Unsupported("merge patch".into());
    }
    let index = args.get("document").and_then(Json::as_u64).unwrap_or(0) as usize;
    let Some(ops) = args.get("patch").and_then(Json::as_array) else {
        return fail("patch-invalid", "patch is not an array", bytes, true);
    };
    // Atomic by construction: edits go to a working copy that is dropped on
    // any failure, because the library applies one edit at a time.
    let mut stream = match Stream::from_slice(bytes) {
        Ok(s) => s,
        Err(e) => {
            return Outcome::Failed(
                Failure {
                    emulated: &["unchanged"],
                    ..error_failure(&e)
                },
                Extra::Output(bytes.to_vec()),
            )
        }
    };
    let Some(mut doc) = stream.document_mut(index) else {
        return fail("patch-path", "no such document", bytes, true);
    };
    for op in ops {
        let name = op.get("op").and_then(Json::as_str).unwrap_or("");
        if !matches!(
            name,
            "add" | "remove" | "replace" | "move" | "copy" | "test"
        ) {
            return fail(
                "patch-invalid",
                format!("unknown operation {name:?}"),
                bytes,
                true,
            );
        }
        let Some(ptr) = op.get("path").and_then(Json::as_str) else {
            return fail("patch-invalid", "operation without a path", bytes, true);
        };
        let Some(tokens) = pointer_tokens(ptr) else {
            return fail("patch-invalid", "not a JSON Pointer", bytes, true);
        };
        if tokens.is_empty() {
            return Outcome::Unsupported("whole-document patch".into());
        }
        let path = match build_path(&doc, &tokens) {
            Ok(p) => p,
            Err(c) => return fail(c, "path does not resolve", bytes, true),
        };
        let exists = doc.get_path(path.clone()).is_some();
        let result = match name {
            "replace" => {
                if !exists {
                    return fail("patch-path", "replace target does not exist", bytes, true);
                }
                let Some(v) = op.get("value") else {
                    return fail("patch-invalid", "replace without a value", bytes, true);
                };
                doc.set_path(path, v).map(|_| ())
            }
            "remove" => {
                if !exists {
                    return fail("patch-path", "remove target does not exist", bytes, true);
                }
                doc.remove_path(path).map(|_| ())
            }
            other => return Outcome::Unsupported(format!("patch operation {other}")),
        };
        if let Err(e) = result {
            let category = match e {
                oneyaml::EditError::NotFound { .. }
                | oneyaml::EditError::NotAMapping { .. }
                | oneyaml::EditError::NotASequence { .. }
                | oneyaml::EditError::IndexOutOfBounds { .. } => "patch-path",
                _ => "other",
            };
            return fail(category, format!("{e}"), bytes, false);
        }
    }
    Outcome::Output(stream.to_bytes())
}

// ---- protocol -------------------------------------------------------------

fn respond(id: &str, o: Outcome) -> String {
    let head = format!("{{\"id\":{}", js(id));
    match o {
        Outcome::Events(l) => format!("{head},\"status\":\"ok\",\"events\":{}}}", js_lines(&l)),
        Outcome::Value(v) => format!("{head},\"status\":\"ok\",\"value\":{v}}}"),
        Outcome::Output(b) => format!(
            "{head},\"status\":\"ok\",\"output\":\"{}\"}}",
            B64.encode(b)
        ),
        Outcome::Unsupported(r) => {
            format!("{head},\"status\":\"unsupported\",\"reason\":{}}}", js(&r))
        }
        Outcome::Failed(f, extra) => {
            let mut err = format!(
                "{{\"category\":{},\"message\":{}",
                js(f.category),
                js(&f.message)
            );
            if let Some((l, c)) = f.position {
                err.push_str(&format!(",\"line\":{l},\"column\":{c}"));
            }
            err.push('}');
            let tail = match extra {
                Extra::None => String::new(),
                Extra::Events(l) => format!(",\"events\":{}", js_lines(&l)),
                Extra::Output(b) => format!(",\"output\":\"{}\"", B64.encode(b)),
            };
            let emu = if f.emulated.is_empty() {
                String::new()
            } else {
                let names: Vec<String> = f.emulated.iter().map(|n| js(n)).collect();
                format!(",\"emulated\":[{}]", names.join(","))
            };
            format!("{head},\"status\":\"error\",\"error\":{err}{tail}{emu}}}")
        }
    }
}

fn js_lines(lines: &[String]) -> String {
    let mut s = String::from("[");
    for (i, l) in lines.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        s.push_str(&js(l));
    }
    s.push(']');
    s
}

fn handle(req: &Json) -> Option<String> {
    match req.get("type").and_then(Json::as_str) {
        Some("hello") => {
            let caps: Vec<String> = CAPABILITIES.iter().map(|c| js(c)).collect();
            let emu: Vec<String> = EMULATES.iter().map(|c| js(c)).collect();
            Some(format!(
                "{{\"type\":\"hello\",\"protocol\":2,\"name\":\"oneyaml-rust\",\"version\":\"1.1.0\",\"language\":\"rust\",\"capabilities\":[{}],\"emulates\":[{}],\"info\":{{\"crates\":\"oneyaml 1.1.0, oneyaml-syntax 0.1.2, oneyaml-core 1.1.0\"}}}}",
                caps.join(","),
                emu.join(",")
            ))
        }
        Some("run") => {
            let id = req.get("id").and_then(Json::as_str).unwrap_or("");
            let bytes = req
                .get("input")
                .and_then(Json::as_str)
                .and_then(|s| B64.decode(s).ok());
            let Some(bytes) = bytes else {
                return Some(respond(
                    id,
                    Outcome::Unsupported("input is not base64".into()),
                ));
            };
            let args = req.get("args").cloned().unwrap_or(Json::Null);
            if args.get("profile").and_then(Json::as_str).unwrap_or("core") != "core" {
                return Some(respond(id, Outcome::Unsupported("profile".into())));
            }
            let outcome = match req.get("op").and_then(Json::as_str) {
                Some("events") => events(&bytes),
                Some("value") => value(&bytes),
                Some("patch") => patch(&bytes, &args),
                Some("dump") => dump(&args, req.get("big_integer").is_some()),
                other => Outcome::Unsupported(format!("operation {other:?}")),
            };
            Some(respond(id, outcome))
        }
        _ => None,
    }
}

/// Whether the JSON text holds an integer literal (no point, no exponent, outside strings)
/// that fits neither i64 nor u64.
fn has_big_integer(text: &str) -> bool {
    let mut in_string = false;
    let mut escaped = false;
    let mut token = String::new();
    for c in text.chars().chain(std::iter::once(' ')) {
        if in_string {
            match (escaped, c) {
                (true, _) => escaped = false,
                (false, '\\') => escaped = true,
                (false, '"') => in_string = false,
                _ => {}
            }
            continue;
        }
        if c.is_ascii_digit() || matches!(c, '-' | '+' | '.' | 'e' | 'E') {
            token.push(c);
            continue;
        }
        if !token.is_empty() {
            let integral = !token.contains(['.', 'e', 'E']);
            if integral && token.parse::<i64>().is_err() && token.parse::<u64>().is_err() {
                return true;
            }
            token.clear();
        }
        if c == '"' {
            in_string = true;
        }
    }
    false
}

fn serve() {
    let stdin = std::io::stdin();
    let mut out = std::io::stdout().lock();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { return };
        let Ok(mut req) = serde_json::from_str::<Json>(&line) else {
            return;
        };
        if line.contains("\"dump\"") && has_big_integer(&line) {
            req["big_integer"] = Json::Bool(true);
        }
        if req.get("type").and_then(Json::as_str) == Some("bye") {
            return;
        }
        if let Some(r) = handle(&req) {
            if writeln!(out, "{r}").and_then(|_| out.flush()).is_err() {
                return;
            }
        }
    }
}

fn main() {
    // Deep inputs are walked recursively by this adapter's value writer, so
    // give it a large stack. The library itself does not recurse on input.
    let t = std::thread::Builder::new().stack_size(1 << 30).spawn(serve);
    if let Ok(h) = t {
        let _ = h.join();
    }
}
