# Benchmark adapters

A benchmark adapter connects one language's libraries to the kit's benchmark harness. It
is a **harness program in the language's own benchmark style** that runs in-process, so
the numbers include no process start or serialisation cost. This page is the contract. A
new language adds a directory and implements it; the driver, the workloads, the verdicts
and the reports do not change.

Rust is the first implementation, `bench/rust/`; Go is the second, `bench/go/`.

## Layout

```
bench/<language>/
  harness.json     how to build and start the harness, and how to count instructions
  known.json       the known deviations of this language's libraries
  ...              the harness itself, with its own lock file pinning every library
```

`harness.json`:

```json
{
 "format": "oneyaml-kit-bench-harness/1",
 "language": "rust",
 "build": ["cargo", "build", "--release", "--locked", "--quiet"],
 "build_env": {"CARGO_TARGET_DIR": "{build}"},
 "command": ["{build}/release/oneyaml-bench-rust"],
 "counters": {"tool": "callgrind", "region": "oneyaml_bench_region"}
}
```

`{build}` is the build directory the driver chooses, so build output never lands in the
repository. `build` runs in the harness directory. `counters` is optional.

## Command line

```
<command> run  --workloads DIR --out FILE --mode quick|full
               [--library ID]... [--task ID]... [--workload ID]... [--skip LIB/TASK/WORKLOAD]...
<command> once --workloads DIR --library ID --task ID --workload ID
```

`run` measures every cell: for each workload in `DIR/index.json`, for each task the
workload lists, for each library (filters narrow the set). `once` runs one iteration of
one cell for instruction counting, with no guard and no clock. Exit status 0 is success;
2 means the harness could not start. Any other status, a signal or a stall is a crash, and
the driver restarts `run` with `--skip` for the cells already begun.

## The workload directory

`DIR/index.json` lists the workloads; `DIR/<id>/` holds `input.yaml`, `workload.json`,
`expected.json` and, where they exist, `expected.merged.json`, `edit.json`,
`edit.expected.yaml`, `patch.json` and `patch.expected.yaml`. The layout is documented at the top of
`bench/oneyaml_bench/workloads.py`; the harness only reads it.

- `input.yaml` is the exact input bytes.
- `expected.json` is the value: the document, or an array of documents when
  `workload.json` says `documents` is more than one.
- `edit.json` and `patch.json` hold `ops` in RFC 6902 form. `patch.json` also has
  `failing_ops`: the same patch with a step that cannot apply.
- A pointer token is an index where the value at that place is a sequence and a key where
  it is a mapping. The harness resolves this against `expected.json`.

## Output

`--out` receives JSON lines, flushed after each line:

```json
{"type": "harness", "name": "...", "version": "...", "language": "rust", "runtime": "...", "target": "...",
 "flags": {}, "mode": "quick", "config": {"warmup_ms": 2, "samples": 5, "sample_target_ms": 1},
 "timer": "...", "memory": "...", "selftest_build": false,
 "libraries": [{"id": "oneyaml", "package": "oneyaml", "version": "1.1.0", "role": "own", "settings": {}}]}
{"type": "begin", "cell": "oneyaml/generic-load/gha-workflow"}
{"type": "cell", "library": "oneyaml", "task": "generic-load", "workload": "gha-workflow",
 "input_bytes": 9656, "status": "ok", "detail": "", "variant": "spec",
 "time": {"iters_per_sample": 7, "samples": 5, "median_ns": 0, "mad_ns": 0, "min_ns": 0, "max_ns": 0, "samples_ns": []},
 "memory": {"method": "counting global allocator", "peak_bytes": 0, "allocated_bytes": 0, "allocations": 0}}
```

- `role` is `own` for the family's library, `competitor` for the others. Libraries versions
  come from the lock file, not from the source.
- `begin` comes before the cell starts. It is how the driver names a cell that killed the
  process.
- `status` is `ok`, `differs`, `rejects`, `unsupported`, `unjudged` or `error`. Only `ok`
  has `time` and `memory`. Every other status has a `detail`. `unjudged` is for a dump whose
  output the harness's reference parsers read differently; it is not a verdict on the library.
- `variant` names which accepted expectation matched: `spec`, `merged` (`<<` applied while
  loading), `bytes-only` (a document with no value to compare, judged on its bytes) or
  `value-only` (a patch baseline that works on JSON and so cannot keep the text, judged on the
  value its output holds; see the patch rule below).
- `memory.method` says how the figures were taken, so numbers from different methods are
  not mixed in one table (see [Memory](#memory)).

## What the harness must do

1. **Guard first.** Run the cell's code once, convert the result to the common form, and
   judge it with the complete comparison: all of the value, every output byte. Time it
   only if it passed. The code judged and the code timed must be the same code.
2. **Convert outside the clock.** Conversion to the common JSON form is for the guard and
   never timed.
3. **Build the starting state outside the clock** and fresh for every iteration, and release
   it after the clock stops; drop each result inside the clock.
4. **Judge the whole answer.**
   - `typed-load` and `generic-load`: value equality, where an integer is not the float with
     the same digits and `"1"` is not `1`. For a stream, an array of documents.
   - `dump`: parse the output with two independent reference parsers; both must read the
     expected value (or the merged value, as variant `merged`). If the two disagree with each
     other, or only one accepts the text, report `unjudged`; if neither accepts it, `differs`.
   - `document-parse`: the value (if the library can give one) and the unchanged bytes.
   - `unchanged-emit`, `edit-script`, `patch`: exact bytes. There is one accepted outcome; a
     library that restyles the edited value or anything around it is `differs`.
   - `patch`: also apply `failing_ops` and require an error and an unchanged document.
5. **Report, never hide.** A refusal is `rejects`, a missing capability is `unsupported`
   with its reason, a panic or exception is `rejects`. Catch what the language can catch.
6. **Warm up and repeat.** Warm up, take at least five samples in quick mode and 30 in
   full mode, report the median and the median absolute deviation, and keep the samples.
7. **Measure memory in a separate run** so tracking never slows the timed ones, and report
   peak live bytes (including the starting state), bytes requested by the task, and the
   number of allocations.
8. **Use the entry point a user of that task would use**, in the language's idiom, and
   record any setting that matters in the library's `settings`. Read the library's
   documentation; do not hand-tune a competitor into a slower shape.
9. **Record the build**: compiler or runtime version, optimisation flags, target.
10. **Exclude test controls from a measurement build.** The harness's own tests use
    deliberately wrong and refusing fake libraries (Rust: the `selftest` feature). They must
    not be compiled into a build that produces numbers; `selftest_build` says which it is.

## Memory

Every `ok` cell carries `memory.method`, and the report prints it. Figures from different
methods are different quantities, and a table must not mix them. All three figures are
for one run of the task, taken in a separate pass from the timed ones:

- `peak_bytes`: the high-water mark of **live managed heap** during the run, counting the
  starting state the task is given (a parsed document, say), not the process's resident set;
- `allocated_bytes`: bytes the task itself requested, cumulative;
- `allocations`: the number of allocation calls the task made.

| Language | `method` | How |
|---|---|---|
| Rust | `counting global allocator` | a `GlobalAlloc` wrapper counts, switched on for the one pass; exact |
| Go | `runtime.MemStats, sampled` | The **peak** pass: build the starting state, `runtime.GC()`, then run the task once with a sampler goroutine reading `HeapAlloc` every millisecond; `peak_bytes` is the highest `HeapAlloc` seen (the reading right after the run counts too, because a task shorter than the sampling interval is not seen by the sampler) **less the `HeapAlloc` the process held before the starting state was built**, so the harness's own data is not charged to the task. `HeapAlloc` includes garbage not yet collected, so it is an upper bound on live bytes at the moment of the reading. The **allocation** figures, `allocated_bytes` (ΔTotalAlloc) and `allocations` (ΔMallocs), are `testing.B`'s `B/op` and `allocs/op` averaged over all the timed samples, because a library that keeps buffers in a pool allocates fewer bytes per run the longer the heap stays unchanged, and one cold run is not the steady state. A separate pass averages the same two counters over up to 64 runs, and the cell fails (`error`) when the number of calls differs by more than 5% or the bytes by more than 25% |
| .NET | `GC.GetAllocatedBytesForCurrentThread` | `GC.Collect()` then read `GC.GetTotalMemory(true)` for the base; `allocated_bytes` from `GC.GetAllocatedBytesForCurrentThread()` before and after the task; `peak_bytes` from `GC.GetTotalMemory(false)` sampled by a timer on a second thread, or from GC events (`System.Diagnostics.Tracing` `GCHeapStats`); `allocations` from the `GCAllocationTick` event count or `null` if unavailable |

A field a runtime cannot measure is `null` and the report shows `n/a`; the method string
says what was used. Never fill a gap with a different quantity under the same name.

## The native harness in each language

The reference harness times with its own loop, because it must run a guard per cell, a
memory pass, batches of prepared state and a machine-readable record, which a general
benchmark framework does not offer. Go and .NET harnesses can use the native tools
for the timing loop and still meet this contract:

| Language | Timing | Counters |
|---|---|---|
| Rust | own loop on `std::time::Instant` | callgrind (`once`) |
| Go | `testing.B` driven through `testing.Benchmark`, so the harness controls the output; `benchstat` over repeated runs for the spread | allocation counts |
| .NET | BenchmarkDotNet with a custom exporter that writes these records | allocated bytes |

Whatever the tool, the cell record above is what leaves the harness.

### Go: set-up outside the clock

`testing.B` calls the benchmark function with a growing `b.N`. Inside it, build `b.N`
starting states, call `b.ResetTimer()`, run the `b.N` tasks over the states, and call
`b.StopTimer()` before returning, so building and releasing the states stay outside the
clock. Call `b.ReportAllocs()`. Do not wrap each iteration in `StopTimer`/`StartTimer`: the
pair costs about a microsecond and swamps short tasks. Cap the batch by the heap the states
hold (as the Rust harness does), running several functions of a smaller `b.N` when needed,
and take the spread across repeated runs with `benchstat`.

Go chunks the batch as the paragraph above says: inside the benchmark function the states of
one chunk are built with the clock stopped, the chunk runs with the clock running, and
`runtime.GC()` runs between chunks with the clock stopped, so the previous chunk's garbage is
not collected on the clock. The chunk is sized from the heap one state holds, at most 64 MiB in
quick mode and 512 MiB in full mode. `testing.Benchmark` is called once for the warm-up and once
per sample with `-test.benchtime` set to the mode's warm-up and sample target, and each call
yields one sample, `T/N`.

### Go: libraries whose API takes and returns text

A patcher such as palantir's `yamlpatch` takes the text and returns text, so there is no
parsed document to start from: parsing and writing are inside its timed work, and the cell's
settings say `state: the text`. They are compared with each other, not with a library that
starts from a parsed document.

A baseline that works on JSON (evanphx's json-patch behind `sigs.k8s.io/yaml`) cannot keep the
text of what it patches, so its output can never be the expected bytes. It is the one library
that may declare its patch output `value-only`: when the bytes are not the expected ones, the
output and the expected bytes are both read by the two reference parsers, and the values must
be equal; the cell is then `ok` with variant `value-only`, which is a known deviation and is
labelled in the report. A wrong value, or a failing patch that does not fail or changes the
text, is still `differs`.

### .NET: set-up outside the clock

Use `[IterationSetup]` to build the starting states and give the benchmark a fixed
`InvocationCount` (and `UnrollFactor = 1`) so that the setup produces exactly as many states
as the iteration consumes; BenchmarkDotNet warns that `IterationSetup` is only reliable for
benchmarks of at least about 100 ms per iteration, so for shorter tasks run a loop of `n`
tasks inside one invocation over a batch built in `IterationSetup`, and report the time per
task. `[IterationCleanup]` releases the states after the clock stops. Set
`Job.WithGcServer(false).WithGcConcurrent(false)` so a background GC does not land in the
sample, and record the job in `flags`.

### Announcing a cell: the `begin` record

The driver finds the cell that killed a process from the last `begin` line, so the harness
must write and flush it before any work for the cell. In Go print it from the harness's
main loop before calling `testing.Benchmark`. In .NET BenchmarkDotNet runs each benchmark
in a child process by default and reports a crash itself; implement `IEventProcessor` (or an
`IConfig` with a custom `IColumnProvider` plus a `BenchmarkRunInfo` hook) to write `begin`
from `OnStartBuildStage`/`OnStartRunBenchmark` to the output file, flushing, or run each cell
with `--inProcess` and write it from the benchmark's `[GlobalSetup]`. Either way `begin`
must name `library/task/workload` exactly.

## Rust: adding a library

Implement `Library` in `bench/rust/src/libs/<name>.rs` and add it to `libs::all()` and to
`Cargo.toml` with an exact `=` version. For each task return a runner built with
`runner(setup, run, product)` or `Err(Unsupported(reason))`:

- `setup` makes one starting state, outside the clock;
- `run` is the timed work and returns `Result<Output, String>` (an error is a rejection);
- `product` converts the output for the guard.

`bench/rust/build.rs` exports the locked version of each package, so `package()` can
return it with `env!("LOCKED_<NAME>")`.

## Go: adding a library

A library is a `yamlLib` in `bench/go/libs_*.go`, registered in `libs.go`, with its module
pinned in `bench/go/go.mod`. It fills in only what the library really has: `unmarshal` (one
document, also used for the typed models in `typed.go`), `decoder` for a stream, `marshal`, a
`doc` (`parse`, `value`, `emit`, `prepareEdit`) when it has a lossless document, and a `patch`.
What it leaves out is `unsupported`, with the reason it gives in `whyNo...`. The version in the
results is read from the build, so it is the one `go.sum` locks. The reference parsers
(`referee.go`) read a dumped document back.

The harness builds without the control libraries, which are compiled only with the `selftest`
tag and are never in a build that produces numbers.

## Known deviations

`bench/<language>/known.json`:

```json
{"format": "oneyaml-kit-bench-known/1", "language": "rust", "entries": [
  {"cell": "oneyaml/edit-script/reference-b", "status": "differs", "reason": "why"},
  {"cell": "yaml-edit/document-parse/gha-workflow", "status": "ok", "variant": "bytes-only", "reason": "why"}]}
```

Every cell that is `differs`, `rejects`, `unjudged` or `error`, or that matched a variant other
than `spec`, must be listed with a reason. `unsupported` cells are capabilities and need no
entry. A run fails when a deviation is unlisted or a listed one no longer happens.
