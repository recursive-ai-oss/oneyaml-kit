//! The contract between the harness and a library: tasks, workloads, and the three
//! small traits a library implements (see docs/benchmark-adapters.md).

use serde_json::Value;
use std::collections::BTreeMap;
use std::path::Path;

/// The seven tasks. Each is defined in docs/benchmarks.md; the harness times exactly the
/// code the correctness guard checked.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Task {
    TypedLoad,
    GenericLoad,
    Dump,
    DocumentParse,
    UnchangedEmit,
    EditScript,
    Patch,
}

impl Task {
    pub const ALL: [Task; 7] = [
        Task::TypedLoad,
        Task::GenericLoad,
        Task::Dump,
        Task::DocumentParse,
        Task::UnchangedEmit,
        Task::EditScript,
        Task::Patch,
    ];
    pub fn id(self) -> &'static str {
        match self {
            Task::TypedLoad => "typed-load",
            Task::GenericLoad => "generic-load",
            Task::Dump => "dump",
            Task::DocumentParse => "document-parse",
            Task::UnchangedEmit => "unchanged-emit",
            Task::EditScript => "edit-script",
            Task::Patch => "patch",
        }
    }
    pub fn parse(s: &str) -> Option<Task> {
        Task::ALL.into_iter().find(|t| t.id() == s)
    }
}

/// One edit or patch step, as in RFC 6902.
#[derive(Clone, Debug)]
pub struct Op {
    pub op: String,
    pub path: String,
    pub value: Option<Value>,
}

impl Op {
    fn from_json(v: &Value) -> Op {
        Op {
            op: v["op"].as_str().unwrap_or_default().to_string(),
            path: v["path"].as_str().unwrap_or_default().to_string(),
            value: v.get("value").cloned(),
        }
    }
}

/// A workload as the guard and the libraries see it. Everything is leaked on load: a
/// workload lives for the whole run and the libraries' closures then borrow it for free.
pub struct Workload {
    pub id: String,
    pub input: &'static [u8],
    pub input_str: &'static str,
    pub documents: usize,
    pub tasks: Vec<Task>,
    pub typed_model: Option<String>,
    /// Expected values by variant name: "spec" first, then e.g. "merged".
    pub expected: Vec<(String, Value)>,
    pub edit_ops: Vec<Op>,
    /// The expected output of the edit script, as ("spec", bytes), or empty when there is none.
    pub edit_expected: Vec<(String, &'static [u8])>,
    // Read by libraries that gain a patch engine; none has one yet.
    #[allow(dead_code)]
    pub patch_ops: Vec<Op>,
    #[allow(dead_code)]
    pub patch_failing_ops: Vec<Op>,
    pub patch_expected: Vec<(String, &'static [u8])>,
    #[allow(dead_code)]
    pub meta: Value,
}

fn leak(v: Vec<u8>) -> &'static [u8] {
    Box::leak(v.into_boxed_slice())
}

fn read_json(p: &Path) -> Result<Value, String> {
    let b = std::fs::read(p).map_err(|e| format!("{}: {e}", p.display()))?;
    serde_json::from_slice(&b).map_err(|e| format!("{}: {e}", p.display()))
}

impl Workload {
    pub fn load(dir: &Path) -> Result<Workload, String> {
        let meta = read_json(&dir.join("workload.json"))?;
        let input = leak(std::fs::read(dir.join("input.yaml")).map_err(|e| e.to_string())?);
        // The libraries here take text; a workload with other encodings needs its own task.
        let input_str: &'static str = std::str::from_utf8(input)
            .map_err(|e| format!("{}: input is not UTF-8: {e}", dir.display()))?;
        let mut tasks = vec![];
        for t in meta["tasks"].as_array().into_iter().flatten() {
            if let Some(t) = t.as_str().and_then(Task::parse) {
                tasks.push(t);
            }
        }
        let mut expected = vec![("spec".to_string(), read_json(&dir.join("expected.json"))?)];
        if dir.join("expected.merged.json").exists() {
            expected.push((
                "merged".to_string(),
                read_json(&dir.join("expected.merged.json"))?,
            ));
        }
        let ops = |name: &str, key: &str| -> Result<Vec<Op>, String> {
            let p = dir.join(name);
            if !p.exists() {
                return Ok(vec![]);
            }
            let v = read_json(&p)?;
            Ok(v[key]
                .as_array()
                .into_iter()
                .flatten()
                .map(Op::from_json)
                .collect())
        };
        // Only the plain outcome counts: an edit changes the value and nothing else, so a
        // library that restyles the rest of the text is wrong, not "also acceptable".
        let variants = |stem: &str| -> Vec<(String, &'static [u8])> {
            std::fs::read(dir.join(format!("{stem}.expected.yaml")))
                .ok()
                .map(|b| ("spec".to_string(), leak(b)))
                .into_iter()
                .collect()
        };
        Ok(Workload {
            id: meta["id"].as_str().unwrap_or_default().to_string(),
            input,
            input_str,
            documents: meta["documents"].as_u64().unwrap_or(1) as usize,
            tasks,
            typed_model: meta["typed_model"].as_str().map(str::to_string),
            expected,
            edit_ops: ops("edit.json", "ops")?,
            edit_expected: variants("edit"),
            patch_ops: ops("patch.json", "ops")?,
            patch_failing_ops: ops("patch.json", "failing_ops")?,
            patch_expected: variants("patch"),
            meta,
        })
    }

    /// The value to start from for a path lookup: the base expectation.
    pub fn base_value(&self) -> &Value {
        &self.expected[0].1
    }
}

/// What a library produced for the guard to judge.
pub enum Product {
    /// A tree, in the common JSON form (a list of documents when there are several).
    Value(Value),
    /// Output bytes.
    Bytes(Vec<u8>),
    /// A parsed document: its value (None for a library with no way to read it back as
    /// data, which is then judged on its bytes alone) and what it writes back unchanged.
    Document {
        value: Option<Value>,
        emitted: Vec<u8>,
    },
}

/// What a library did with the failing patch (the one with a step that cannot apply).
pub struct FailedPatch {
    pub errored: bool,
    /// The bytes the document holds afterwards.
    pub bytes: Vec<u8>,
}

/// Prepared iterations of a task. `run` performs all of them, drops their results, and
/// is the only code the clock sees.
pub trait Batch {
    fn run(&mut self);
}

pub trait Runner {
    /// Runs the task once, as it is timed, and converts the result for the guard. An error
    /// is the library rejecting the input.
    fn check(&self) -> Result<Product, String>;
    /// Builds `n` independent starting states, outside the clock.
    fn prepare(&self, n: usize) -> Box<dyn Batch>;
    /// For patch runners: applies the failing patch and reports the outcome.
    fn check_failing(&self) -> Option<FailedPatch> {
        None
    }
}

/// Why a library has no runner for a task.
pub struct Unsupported(pub &'static str);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    Own,
    Competitor,
    /// A deliberately fake library used by the harness's own tests.
    #[cfg_attr(not(feature = "selftest"), allow(dead_code))]
    Control,
}

impl Role {
    pub fn id(self) -> &'static str {
        match self {
            Role::Own => "own",
            Role::Competitor => "competitor",
            Role::Control => "control",
        }
    }
}

pub trait Library {
    /// Short stable id used in results and reports.
    fn id(&self) -> &'static str;
    /// The crate measured and the version resolved by the lockfile.
    fn package(&self) -> (&'static str, &'static str);
    fn role(&self) -> Role;
    /// Features and settings the measurement relies on, recorded in the results.
    fn settings(&self) -> BTreeMap<&'static str, String> {
        BTreeMap::new()
    }
    fn runner(&self, task: Task, w: &'static Workload) -> Result<Box<dyn Runner>, Unsupported>;
}

// ---- building runners from closures ------------------------------------------------------

/// The timed task: it borrows its starting state, which is released after the clock stops.
type RunFn<S, O> = std::rc::Rc<dyn Fn(&mut S) -> Result<O, String>>;

struct FnRunner<S, O> {
    setup: Box<dyn Fn() -> S>,
    run: RunFn<S, O>,
    product: Box<dyn Fn(O) -> Product>,
    failing: Option<Box<dyn Fn() -> FailedPatch>>,
}

struct FnBatch<S, O> {
    states: Vec<S>,
    run: RunFn<S, O>,
}

impl<S, O> Batch for FnBatch<S, O> {
    fn run(&mut self) {
        for s in self.states.iter_mut() {
            // The result is dropped inside the timed region, as a caller's would be. The
            // starting state is not: it stays in the batch and is released after the clock stops.
            std::hint::black_box((self.run)(std::hint::black_box(s)).ok());
        }
    }
}

impl<S: 'static, O: 'static> Runner for FnRunner<S, O> {
    fn check(&self) -> Result<Product, String> {
        let mut state = (self.setup)();
        let out = (self.run)(&mut state)?;
        Ok((self.product)(out))
    }
    fn prepare(&self, n: usize) -> Box<dyn Batch> {
        Box::new(FnBatch {
            states: (0..n).map(|_| (self.setup)()).collect(),
            run: self.run.clone(),
        })
    }
    fn check_failing(&self) -> Option<FailedPatch> {
        self.failing.as_ref().map(|f| f())
    }
}

/// Builds a runner. `setup` makes one starting state (not timed), `run` is the timed task
/// and borrows that state, so its release happens after the clock stops, and `product`
/// converts the output for the guard (not timed).
pub fn runner<S: 'static, O: 'static>(
    setup: impl Fn() -> S + 'static,
    run: impl Fn(&mut S) -> Result<O, String> + 'static,
    product: impl Fn(O) -> Product + 'static,
) -> Box<dyn Runner> {
    Box::new(FnRunner {
        setup: Box::new(setup),
        run: std::rc::Rc::new(run),
        product: Box::new(product),
        failing: None,
    })
}

#[cfg_attr(not(feature = "selftest"), allow(dead_code))]
/// As [`runner`], for a patch task that also checks the failing patch.
pub fn patch_runner<S: 'static, O: 'static>(
    setup: impl Fn() -> S + 'static,
    run: impl Fn(&mut S) -> Result<O, String> + 'static,
    product: impl Fn(O) -> Product + 'static,
    failing: impl Fn() -> FailedPatch + 'static,
) -> Box<dyn Runner> {
    Box::new(FnRunner {
        setup: Box::new(setup),
        run: std::rc::Rc::new(run),
        product: Box::new(product),
        failing: Some(Box::new(failing)),
    })
}

/// The common form of a document list: the value itself for one document, else an array.
pub fn docs_value(mut docs: Vec<Value>) -> Value {
    if docs.len() == 1 {
        docs.remove(0)
    } else {
        Value::Array(docs)
    }
}

/// Splits an RFC 6901 pointer into unescaped tokens.
pub fn pointer_tokens(ptr: &str) -> Vec<String> {
    if ptr.is_empty() {
        return vec![];
    }
    ptr[1..]
        .split('/')
        .map(|t| t.replace("~1", "/").replace("~0", "~"))
        .collect()
}

/// A step of a path into a document.
#[derive(Clone, Debug, PartialEq)]
pub enum Seg {
    Key(String),
    Index(usize),
}

/// Resolves a pointer against the workload's value: a token is an index where the value
/// there is a sequence and a key where it is a mapping. `last_new` allows the final token
/// to name something that does not exist yet (an `add`).
pub fn pointer_segments(base: &Value, ptr: &str, last_new: bool) -> Result<Vec<Seg>, String> {
    let toks = pointer_tokens(ptr);
    let mut cur = base;
    let mut out = vec![];
    for (i, t) in toks.iter().enumerate() {
        let last = i + 1 == toks.len();
        match cur {
            Value::Array(a) => {
                if t == "-" && last && last_new {
                    return Err("append".into());
                }
                let n: usize = t.parse().map_err(|_| format!("bad index {t:?} in {ptr}"))?;
                out.push(Seg::Index(n));
                cur = a.get(n).ok_or_else(|| format!("no element {n} in {ptr}"))?;
            }
            Value::Object(m) => {
                out.push(Seg::Key(t.clone()));
                match m.get(t) {
                    Some(v) => cur = v,
                    None if last && last_new => return Ok(out),
                    None => return Err(format!("no key {t:?} in {ptr}")),
                }
            }
            _ => return Err(format!("cannot descend into a scalar at {t:?} in {ptr}")),
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn pointer_escapes_and_indices() {
        let v = json!({"a/b": [{"c": 1}], "~": 2});
        assert_eq!(
            pointer_segments(&v, "/a~1b/0/c", false).unwrap(),
            vec![Seg::Key("a/b".into()), Seg::Index(0), Seg::Key("c".into())]
        );
        assert_eq!(
            pointer_segments(&v, "/~0", false).unwrap(),
            vec![Seg::Key("~".into())]
        );
        assert!(pointer_segments(&v, "/nope", false).is_err());
        assert!(pointer_segments(&v, "/nope", true).is_ok());
    }

    #[test]
    fn task_ids_round_trip() {
        for t in Task::ALL {
            assert_eq!(Task::parse(t.id()), Some(t));
        }
    }
}
