# Benchmark adapters

A benchmark adapter connects one language's libraries to the benchmark. It is a **harness
program in the language's own benchmark tool** that runs in-process, so the numbers include no
process start or serialisation cost. This page is the contract. A new language adds a directory
and implements it; the driver, the workloads and the reports do not change.

| Language | Directory | Tool, at its defaults |
|---|---|---|
| Rust | `bench/rust/` | criterion for time; a counting global allocator for allocation |
| Go | `bench/go/` | `testing.Benchmark` with `ReportAllocs` (ns/op, B/op, allocs/op) |
| .NET | `bench/csharp/` | BenchmarkDotNet's default job, run in process, with `MemoryDiagnoser` |

Quick mode shortens the tool's time settings (criterion's warm-up and measurement time, Go's
benchtime, BenchmarkDotNet's dry job) and changes nothing else.

## Layout

```
bench/<language>/
  harness.json     how to build and start the harness
  ...              the harness itself, with its own lock file pinning every library
```

```json
{
 "format": "oneyaml-kit-bench-harness/1",
 "language": "rust",
 "build": ["cargo", "build", "--release", "--locked", "--quiet"],
 "build_env": {"CARGO_TARGET_DIR": "{build}"},
 "command": ["{build}/release/oneyaml-bench-rust"]
}
```

`{build}` is the build directory the driver chooses, so build output never lands in the
repository. `build` runs in the harness directory.

## Command line

```
<command> run --workloads DIR --out FILE --mode quick|full
              [--library ID]... [--task ID]... [--workload ID]... [--skip LIB/TASK/WORKLOAD]...
<command> list
```

`run` measures every cell: for each workload in `DIR/index.json`, for each task the workload
lists, for each library (the filters narrow the set). Exit status 0 is success; 2 means the
harness could not start. Any other status is a crash: the driver records the cell that was
running as `error` and starts `run` again with `--skip` for the cells already begun.

## The workload directory

`DIR/index.json` lists the workloads; `DIR/<id>/` holds `input.yaml`, `workload.json`,
`expected.json` and, where they exist, `expected.merged.json`, `edit.json`, `edit.expected.yaml`,
`patch.json` and `patch.expected.yaml`. The layout is documented at the top of
`bench/oneyaml_bench/workloads.py`; the harness only reads it.

- `input.yaml` is the exact input bytes.
- `expected.json` is the value: the document, or an array of documents when `workload.json`
  says `documents` is more than one.
- `edit.json` and `patch.json` hold `ops` in RFC 6902 form. `patch.json` also has `failing_ops`:
  the same patch with a step that cannot apply.
- A pointer token is an index where the value at that place is a sequence and a key where it
  is a mapping. The harness resolves this against `expected.json`.

## Output

`--out` receives JSON lines, flushed after each line:

```json
{"type": "harness", "name": "...", "version": "...", "language": "rust", "runtime": "...", "target": "...",
 "mode": "quick", "tool": "criterion", "timer": "...", "memory": "...",
 "libraries": [{"id": "oneyaml", "package": "oneyaml", "version": "1.1.0", "role": "own", "settings": {}}]}
{"type": "begin", "cell": "oneyaml/generic-load/gha-workflow"}
{"type": "cell", "library": "oneyaml", "task": "generic-load", "workload": "gha-workflow",
 "input_bytes": 9656, "status": "ok", "detail": "", "variant": "spec",
 "time": {"ns": 149000.0, "stat": "mean"},
 "memory": {"allocated_bytes": 134000, "allocations": 1200}}
```

- `role` is `own` for the family's library, `competitor` for the others. Library versions come
  from the lock file, not from the source.
- `begin` comes before the cell starts. It is how the driver names a cell that killed the process.
- `status` is `ok`, `differs`, `rejects`, `unsupported`, `unjudged` or `error`. Only `ok` has
  `time` and `memory`. Every other status has a `detail`. `unjudged` is for a dump whose output
  the harness's reference parsers read differently; it is not a verdict on the library.
- `time.ns` is the tool's own figure for one run, and `stat` says which (`mean` in all three
  harnesses). A harness may add the tool's other figures (`median_ns`).
- `memory.allocated_bytes` is the bytes one run allocated. `allocations` is the number of
  allocation calls, or null where the tool does not report it (BenchmarkDotNet).
- `variant` names which accepted expectation matched: `spec`, `merged` (`<<` applied while
  loading), `bytes-only` (a document with no value to compare, judged on its bytes),
  `harness-rendered` (an edit whose lookup and new text are the harness's code, because the
  library only replaces a range of characters; the bytes are judged and the work is timed) or
  `value-only` (a patch baseline that works on JSON and so cannot keep the text, judged on the
  value its output holds). Only `spec` and `merged` are called the best of a row, but every ok
  cell is compared with; see [What may be the best](#what-may-be-the-best).
- `settings.state` is `the text` for a library whose API takes and returns text, so it has no
  parsed document to start from and its parsing and writing are inside the timed work. The driver
  marks its cells `"state": "text"`; they are shown, marked, and never called the best of a row.

## What the harness must do

1. **Guard first.** Run the cell's code once, convert the result to the common form, and judge it
   with the complete comparison: all of the value, every output byte. Time it only if it passed.
   The code judged and the code timed must be the same code.
2. **Convert outside the clock.** Conversion to the common JSON form is for the guard and never timed.
3. **Build the starting state outside the clock**, fresh for every run, and release it after the clock
   stops; drop each result inside the clock. A state owns its input: it gets a copy made in setup, in
   whatever form the library takes (bytes, string, stream, file), and no two states, and not the workload,
   share an input object. A document parsed in setup is parsed from that state's own copy. Nothing may be
   shared between states to save setup time, a parsed document included. The harness has a test that fails
   when two states reach the same input buffer.
4. **Judge the whole answer.**
   - `typed-load` and `generic-load`: value equality, where an integer is not the float with the
     same digits and `"1"` is not `1`. For a stream, an array of documents.
   - `dump`: parse the output with two independent reference parsers; both must read the expected
     value (or the merged value, as variant `merged`). If the two disagree with each other, or only
     one accepts the text, report `unjudged`; if neither accepts it, `differs`. The parsers supply
     the structure of the text (nodes, styles, anchors, explicit tags), never its meaning: what a
     plain scalar is comes from a resolver of the harness's own for the YAML 1.2 core schema, and a
     merge key is an ordinary key, never merged.
   - `document-parse`: the value (if the library can give one) and the unchanged bytes.
   - `unchanged-emit`, `edit-script`, `patch`: exact bytes. There is one accepted outcome; a library
     that restyles the edited value or anything around it is `differs`.
   - `patch`: also apply `failing_ops` and require an error and an unchanged document.
5. **Report, never hide.** A refusal is `rejects`, a missing capability is `unsupported` with its
   reason, a panic or exception is `rejects`. Catch what the language can catch.
6. **Use the entry point a user of that task would use**, in the language's idiom, and record any
   setting that matters in the library's `settings`. Read the library's documentation; do not
   hand-tune a competitor into a slower shape.
7. **Record the build**: compiler or runtime version and the tool, in the harness record.
8. **Pin every library** to an exact version in the harness's own manifest and lock file.
9. **Self-test with controls.** A build with the `selftest` feature, tag or property adds
   libraries that answer correctly, wrongly, refuse, or crash. They never appear in a build that
   produces numbers, and the driver's tests run them to prove a wrong answer gets no number.

## What may be the best

A row is always compared with the **strongest competitor in it**. A cell that did less than the
full task carries a mark (†) and its reason: a variant other than `spec` and `merged`, or a text
API. A marked cell is never called the best, but when it is faster than ours, or is the only
competitor, the verdict names it with its mark and a loss to it is a loss. A tie goes to an
unmarked cell. A row has no rival only when no competitor answered correctly. In a language with
no library of ours, the tables name the best library of each row instead, and name a faster
marked cell beside it.

The variant `merged` carries no mark and may be called the best: the value is the expected one
apart from the merge, the work is loading the document, and a library that reads `<<` as an
ordinary key (YAML 1.2) is measured against it honestly. It is labelled in every table.

## Rust: adding a library

Implement `Library` in `bench/rust/src/libs/`, register it in `libs/mod.rs`, and pin its crate
with `=` in `Cargo.toml`. For each task return a runner built from `setup`, `run` and `product`
or an `Unsupported` reason. The version in the results is read from `Cargo.lock` by `build.rs`.

## Go: adding a library

A library is a `yamlLib` in `bench/go/libs_*.go`, registered in `libs.go`, with its module
pinned in `bench/go/go.mod`. It fills in only what the library really has: `unmarshal` (one
document, also used for the typed models in `typed.go`), `decoder` for a stream, `marshal`, a
`doc` (`parse`, `value`, `emit`, `prepareEdit`) when it has a lossless document, and a `patch`.
What it leaves out is `unsupported`, with the reason it gives in `whyNo...`. The reference reading
(`referee.go`) takes the structure of a dumped document from yaml.v3 and goccy and resolves it with
the harness's core-schema resolver.

## .NET: adding a library

Implement `ILibrary` in `bench/csharp/src/Libs/` and add it to `Program.AllLibraries()`, with an
exact version range in `Bench.csproj` (`[1.2.3]`) and a refreshed `packages.lock.json`. For each
task return a `FnRunner<TState, TOut>(setup, run, product)` or throw `Unsupported(reason)`. An
exception from `setup` or `run` is the library refusing the input. A library that can be driven
in more than one way (a serializer, a node model, a syntax tree) is one `id` per way, each with
its own `settings`, so a table row says which API it measured. `Models.cs` holds the typed models.

To measure a checkout of oneyaml-csharp, set `KIT_ONEYAML_CSHARP_SRC` to it before the build;
`KIT_ONEYAML_GO_SRC` does the same for oneyaml-go. Without them the harness measures the
competitors alone.

## A cell that is a known loss or deviation

There is no list to maintain: a cell that is `differs`, `rejects`, `unjudged` or `error` is shown
in the report's table of cells without a number, with the guard's first difference as its reason.
