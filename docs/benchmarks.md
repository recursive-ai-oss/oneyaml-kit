# Benchmarks

The benchmark harness measures YAML libraries, ours and their competitors, on the same
inputs and the same tasks, and generates the tables and charts that library READMEs
embed. It reports wall-clock time and memory, checks every answer before timing it, and
says plainly when a run is only a smoke result.

```sh
bin/bench run                    # quick mode: seconds, writes out/bench/
bin/bench run --mode full        # full mode: minutes, for a quiet host
bin/bench run --task dump --workload gha-workflow   # a slice of either
bin/bench run --counters         # also count instructions with callgrind
bin/bench report out/bench/results.json             # re-render a report
bin/bench workloads              # write the workload set only
bin/bench freeze --check         # verify the real-world expectations
bin/bench baseline out/bench/results.json --out bench/rust/counters.json
bin/bench check out/bench/results.json --baseline bench/rust/counters.json
```

Output goes to `$KIT_OUT/bench` (default `./out/bench`, never committed):
`results.json`, `report.md`, `charts/*.svg`, and the prepared `workloads/`.
Rust build output goes to `$CARGO_TARGET_DIR`, or to `out/bench/target`.

Quick mode is for the inner loop: short warm-up, five short samples per cell. Full mode
is for numbers worth reading: a 300 ms warm-up and 31 samples of about 50 ms per cell, and
the large scaling inputs. Numbers taken on a shared machine are **smoke results** and the
report says so (see [Publishable runs](#publishable-runs)).

## What is measured

### Tasks

Every task is defined so that different libraries do equivalent work. Each cell runs
the same code twice: once for the correctness guard, and then many times under the clock.

| Task | Starting state (not timed) | Timed work | The guard accepts when |
|---|---|---|---|
| `typed-load` | the input text | parse into the language's typed model (structs) | the model, serialised to JSON, equals the expected value |
| `generic-load` | the input text | parse into the library's generic value tree; every document of a stream | the tree, converted to JSON, equals the expected value |
| `dump` | the library's own value tree, parsed from the input | write the tree as YAML text | two independent reference parsers both read the text back to the expected value |
| `document-parse` | the input text | parse into a lossless document (comments, spacing, styles kept) | the document holds the expected value and writes the input back byte for byte |
| `unchanged-emit` | a parsed lossless document | write it back | the bytes equal the input |
| `edit-script` | a parsed lossless document | apply a fixed list of `replace` and `remove` edits through the library's own edit API, write the bytes | the bytes equal the expected bytes exactly |
| `patch` | a parsed lossless document | apply an RFC 6902 patch atomically, write the bytes | the bytes equal the expected bytes, and a second patch with a failing step leaves the document unchanged and reports an error |

The state each task starts from is built fresh for every iteration, outside the clock. The
result of every iteration is dropped inside the clock, as a caller's would be.

A library without a capability gets an `unsupported` cell and the reason. A parser that
discards comments cannot take `document-parse`; that is not a loss, it is a different
tool. Edits and patches are written against the lossless document, so the comparison is
always "the same edit, byte for byte".

### Workloads

Workloads are YAML inputs with everything needed to judge each task. They live in two
places and are written to `out/bench/workloads/` before a run:

- `bench/workloads/real/<id>/`: real files, vendored byte for byte with their source,
  revision and licence (see `bench/workloads/real/SOURCES.md`).
- `bench/oneyaml_bench/gen.py`: deterministic generators for scaling and stress shapes,
  and the two reference edit cases. They are written from scratch and need no licence.
  The generated text and the value it means come from the same Python data, so the
  expectation never comes from a YAML library.

| Group | Workloads |
|---|---|
| Real files | a GitHub Actions workflow, a Compose file, Helm chart values, two OpenAPI descriptions, a multi-document Kubernetes manifest, a Prometheus configuration |
| Shapes at several sizes | typed records, Compose with anchors and merge keys, multi-document Kubernetes, OpenAPI, Helm-style values, a flat wide mapping (the sizes run from 2 KB to 8 MB; the large ones only in full mode) |
| Stress | nested block and flow collections to depth 100, very long scalars of every style, flow-heavy data, anchors and aliases, Unicode, every scalar style and chomping mode |
| Reference edits | the Minimal-diff reference cases: a block literal set to text with and without a final newline |

### Expectations

- The value of a generated workload comes from the generator.
- The value of a real file is frozen (`bin/bench freeze`) only when two independent
  YAML 1.2 parsers read the file to the same value. The freeze also lists every place a
  YAML 1.1 reader differs, so each scalar whose meaning depends on the schema has been
  looked at. For example the GitHub Actions key `on` is the string `"on"` in YAML 1.2.
- The bytes expected after an edit or a patch are written as exact text substitutions on
  the input (`edits` in `workloads.py` and `gen.py`). Each substitution must match
  exactly once, so a stale one fails loudly. No library produces an expected byte.
- Where two outcomes are acceptable the workload carries both and the result names which
  one the library produced: `merged` (a library that applies `<<` merge keys, a YAML 1.1
  extension; YAML 1.2 reads `<<` as a plain key) and `restyled` (an edited value written
  in quotes although the original plain style could hold it). Only the plain outcome is
  `spec`. Restyled and merged results are listed as deviations.

### Metrics

- **Time:** wall-clock time of one operation. A sample runs a batch of prepared
  iterations and divides by the batch size, with the batch sized so a sample lasts a
  target time. The reported time is the median of the samples, the spread is the median
  absolute deviation (shown as a percentage), and all samples are kept in the result.
- **Memory:** one extra tracked run of the task, through a counting global allocator.
  *Peak* is the high-water mark of live heap bytes, including the state the task starts
  from (for example the parsed document of an emit). *Allocated* is the bytes the task
  itself requested, and *allocations* the number of allocation calls. Stacks and memory
  mapped outside the allocator are not seen.
- **Instructions** (`--counters`): instructions executed by one iteration, counted by
  callgrind restricted to the measured region.

Allocation counts and heap figures repeat to within a fraction of a percent for a given
input and library version, so they gate CI: `bench/rust/counters.json` holds the figures
of our library for every quick-mode cell, and `bin/bench check` fails when one grows by
more than 1% (2% for instructions) or when an entry no longer matches a measured cell.
Refresh it with `bin/bench baseline` when a change is meant to alter them. Wall-clock time
is the number that goes into a README.

### Verdicts

For each row the reference is the **best competitor in that row on that metric**: the
fastest for time, the smallest peak for memory. Our library's ratio is its value divided
by the reference's, so below 1.0 is better. A gap is a **tie** when it is smaller than
the noise: 2% or three times the larger relative spread for time, 1% for memory.
Otherwise it is a **win** or a **loss**. Losses are marked in the tables and the charts.

## Correctness guard

A library gets a number only if its answer was right.

- `differs`: it produced an answer that is not the expected one. The cell shows the
  first difference.
- `rejects`: it refused an input the specification accepts, or panicked.
- `unsupported`: it has no such capability, with the reason.
- `error`: the harness could not measure it (the process died or stalled). The driver
  restarts the harness past that cell.

Cells that are not `ok` never carry a time or a memory figure; the result validator and
the harness's own tests enforce that. `bench/<language>/known.json` lists every cell that
is a deviation. A new deviation, or an entry that no longer holds, fails the run.

## Publishable runs

Every result records the date, versions of every library and of the harness, the compiler
or runtime, build flags, the CPU, the operating system, the governor, and the host load.
While a run is in progress the driver samples how many cores are busy with work other than
the harness. A run is labelled **publishable** only if:

- it is a full run that finished every cell it set out to measure;
- other work kept at most 1.0 cores busy for 95% of the samples and at most 4.0 at the
  worst sample;
- the one-minute load average was at most 3.0 at the start and at the end (the harness's
  own core included);
- at least five load samples were taken.

Otherwise the report is headed **SMOKE RESULT, NOT PUBLISHABLE** and lists the reasons.
The thresholds are in `PUBLISH_RULE` in `bench/oneyaml_bench/results.py` and are recorded
in each result. They say nothing about CPU frequency scaling or isolated cores; those are
recorded (governor, boost, `--pin`) but not checked.

## Result format

`results.json`, format `oneyaml-kit-bench/1`:

```json
{
 "format": "oneyaml-kit-bench/1",
 "run": {"date": "...", "mode": "quick", "seconds": 31.0, "restarts": 0, "counters": false,
         "environment": {"cpu": "...", "cpus": 32, "os": "...", "governor": "...", "...": "..."},
         "publishability": {"publishable": false, "label": "smoke", "reasons": ["..."], "rule": {}, "measured": {}},
         "load": {"before": 1.8, "after": 2.2}},
 "harnesses": {"rust": {"name": "oneyaml-bench-rust", "version": "0.1.0", "runtime": "rustc ...",
                        "flags": {}, "config": {}, "libraries": [{"id": "oneyaml", "package": "oneyaml",
                        "version": "1.1.0", "role": "own", "settings": {}}]}},
 "workloads": [{"id": "gha-workflow", "kind": "real-world", "input_bytes": 9656, "source": {}, "tasks": []}],
 "cells": [
  {"language": "rust", "library": "oneyaml", "role": "own", "task": "generic-load", "workload": "gha-workflow",
   "status": "ok", "variant": "spec", "input_bytes": 9656,
   "time": {"median_ns": 149000.0, "mad_ns": 800.0, "min_ns": 148000.0, "max_ns": 152000.0,
            "samples": 5, "iters_per_sample": 7, "samples_ns": []},
   "memory": {"peak_bytes": 51000, "allocated_bytes": 134000, "allocations": 1200},
   "instructions": null},
  {"language": "rust", "library": "yaml-edit", "role": "competitor", "task": "edit-script",
   "workload": "reference-b", "status": "differs", "detail": "byte 26: got ..., expected ..."}
 ]
}
```

## Reports

`bin/bench run` and `bin/bench report` write `report.md` and `charts/`. Everything in them
comes from the result file: the environment, the libraries and versions, one table per
task for time and one for memory with every library and "ours against the best competitor"
with its verdict, a bar chart for each, the cells without a number with their reasons, and
the workloads with their sources and licences. Charts are zero-based and linear, drawn in a
light and a dark variant, and their alt text carries every number. A bar longer than the
axis is clipped and marked. The report passes the same public-text check as hand-written
text.

## Adding a library or a language

See [benchmark-adapters.md](benchmark-adapters.md).
