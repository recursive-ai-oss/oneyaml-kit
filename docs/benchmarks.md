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
Build output goes to `$CARGO_TARGET_DIR`, or to `out/bench/target`: the Rust harness is built there by cargo, and the Go harness binary (`oneyaml-bench-go`) is written beside it.

Quick mode is for the inner loop: short warm-up, five short samples per cell. Full mode
is for numbers worth reading: a 300 ms warm-up and 31 samples of about 50 ms per cell (a full run never
takes fewer than 30 samples in any cell; fewer are for quick mode only), and
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
| `dump` | the library's own value tree, parsed from the input | write the tree as YAML text | two independent reference parsers both read the text back to the expected value (or, when the library merges `<<` keys while loading, to the merged value) |
| `document-parse` | the input text | parse into a lossless document (comments, spacing, styles kept) | the document holds the expected value and writes the input back byte for byte |
| `unchanged-emit` | a parsed lossless document | write it back | the bytes equal the input |
| `edit-script` | a parsed lossless document | apply a fixed list of `replace` and `remove` edits through the library's own edit API, write the bytes | the bytes equal the expected bytes exactly: only the edited value changes, and it keeps its own style |
| `patch` | a parsed lossless document | apply an RFC 6902 patch atomically, write the bytes | the bytes equal the expected bytes, and a second patch with a failing step leaves the document unchanged and reports an error |

The state each task starts from is built fresh for every iteration, outside the clock, and
is released after the clock stops. The result of every iteration (a value tree, an output
buffer) is dropped inside the clock, as a caller's would be. Edit paths are resolved from the
workload's pointers once, before the clock, into the library's own path syntax.

Each library is driven through the API its documentation shows for the task. Where one API
stops short of an edit the workload needs, the harness uses the same library's next API, never
a reduced edit: yaml-edit's path API handles mapping keys, and its node API (`Sequence::set`,
`Sequence::remove`, `Document::as_sequence`) handles sequence items and sequence roots.

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
- An edit has one acceptable outcome. A library that rewrites the text of the edited value
  in another style (quoting `1.1.0`, say) or changes anything else is `differs`.
- Two outcomes are accepted where the specification leaves a choice, and the result names
  which one the library produced: `merged` (a library that applies `<<` merge keys while
  loading, a YAML 1.1 extension; YAML 1.2 reads `<<` as a plain key) for `generic-load`,
  `dump` and `document-parse`. Only the plain outcome is `spec`. A `merged` result, and a
  `bytes-only` one (a document judged on its bytes because the library gives no value to
  compare), are listed as known deviations. A third, `value-only`, exists for one purpose: the
  baseline patcher that works on JSON and cannot keep the text (see
  [benchmark-adapters.md](benchmark-adapters.md)); its output is judged on the value it holds.
  A fourth, `harness-rendered`, marks an edit whose lookup and new text are written by the harness
  because the library only replaces a range of characters; the work is timed, and the cell is
  listed as a known deviation.

### Metrics

- **Time:** wall-clock time of one operation. A sample runs a batch of prepared
  iterations and divides by the batch size, with the batch sized so a sample lasts a
  target time. The reported time is the median of the samples, the spread is the median
  absolute deviation (shown as a percentage), and all samples are kept in the result.
- **Memory:** one extra tracked run of the task, through a counting global allocator.
  *Peak* is the high-water mark of live heap bytes, including the state the task starts
  from (for example the parsed document of an emit). *Retained*, where a harness measures it,
  is what a run leaves live: the live heap one full collection after the run, less the live heap
  just before it, with the starting state held in both readings. It has this one definition in
  every harness (pools and caches show up in it). *Allocated* is the bytes the task
  itself requested, and *allocations* the number of allocation calls. Stacks and memory
  mapped outside the allocator are not seen.
  The Go harness cannot count through an allocator, so it reads `runtime.MemStats` instead
  and says so in each cell's method (`runtime.MemStats, sampled`); its peak is the highest
  heap reading seen and is not the same quantity as the Rust one. Its allocation figures are
  `testing.B` per-op averages over all the timed runs, so a cost a library pays only now and then (a refill
  after a collection, a periodic flush) is spread over the runs as a user's program spreads it. After
  each collection the harness makes one unmeasured run, so pools are full when counting starts; the
  cost of that first run is reported separately as `first_run_allocations`, in the result file only.
  What a library keeps in its pools shows in `retained_bytes`. The exact method is in
  [benchmark-adapters.md](benchmark-adapters.md#memory), and a table never mixes methods.
- **Memory in .NET** is not an allocator count: allocated bytes are exact, the peak is the starting
  state plus every byte the task requested, measured in a no-GC region so that it does not depend on the
  collector's earlier state (see [benchmark-adapters.md](benchmark-adapters.md#memory)), `retained_bytes`
  is what the library keeps after a run (pools, caches), and the number of allocation calls is not
  available. Do not compare it with the Rust or Go figures. The allocated bytes of one process are
  exact and repeat to the byte, but they are not the same in every process: the runtime's dynamic
  profile-guided optimisation decides, from a sample of the calls it saw, which objects the compiled
  code keeps on the stack, and that sample differs from run to run. Ten identical runs of one cell
  gave up to 5.5% spread in quick mode and 11% in full mode, in steps of one allocation site (for
  YamlDotNet's generic load of `gha-workflow`, 420,816, 444,016 or 467,216 bytes). Treat .NET memory
  ratios closer than about 10% as ties; the verdict's tie band for memory (1%) is too narrow for
  this runtime.
- **Instructions** (`--counters`): instructions executed by one iteration, counted by
  callgrind restricted to the measured region.

Allocation counts and heap figures repeat to within a few percent for a given
input and library version (one oneyaml edit path varies by up to 2% with the hash seed), so they gate CI: `bench/rust/counters.json` holds the figures
of our library for every quick-mode cell, and `bin/bench check` fails when one grows by
more than 3% (allocation calls), 1% (bytes) or 2% (instructions), or when an entry no
longer matches a measured cell.
Refresh it with `bin/bench baseline` when a change is meant to alter them. Wall-clock time
is the number that goes into a README.

### Verdicts

For each row the reference is the **strongest competitor in that row on that metric**: the
fastest for time, the smallest peak for memory, whatever it did. Our library's ratio is its value divided
by the reference's, so below 1.0 is better. A gap is a **tie** when it is smaller than
the noise: 2% or three times the larger relative spread for time, 1% for memory.
Otherwise it is a **win** or a **loss**. Losses are marked in the tables and the charts.

A cell judged more loosely (`bytes-only`, `value-only`, `harness-rendered`) or from a library that takes
and returns text (`state: the text`) is shown with a † and is never called the best. It is still compared
with: when it is faster than ours, or is the only competitor, the verdict names it with its mark ("loss vs
yaml-edit †, bytes-only") and a loss to it is a loss in the tally and the weakest row. A row has "no rival"
only when no competitor answered correctly. `merged` carries no mark and may be called the best on every
task where it occurs (`generic-load`, `dump` and `document-parse`), and is labelled wherever it appears.
The rule and the reasons are in
[benchmark-adapters.md](benchmark-adapters.md#what-may-be-the-best).

### A language without a library of ours

The verdicts compare our library with the strongest competitor. A language whose harness has no library of
ours (the .NET harness, until there is one) has no such comparison. Its tables name the best library in
each row instead (a faster † cell is named beside it, marked), its summary lists for each library the
geometric mean of its value divided by the best full-task library's in the same row (1.0× is the best), the
number of rows it won and the number of rows in which a † cell was ahead of it, and its charts draw that mean
per library. That is the baseline a library of ours has to beat. Because an average over a library's own
rows flatters a library that fails its slow rows, the summary also gives each library's mean over only the
rows in which every library that can set the best has a number, with the number of those rows. A library
that is never called the best because of the rule above is listed with its ratio and the reason.

## Correctness guard

A library gets a number only if its answer was right.

- `differs`: it produced an answer that is not the expected one. The cell shows the
  first difference.
- `rejects`: it refused an input the specification accepts, or panicked.
- `unsupported`: it has no such capability, with the reason. A refusal of an edit that the
  library's other API can make is not a capability gap, so the harness drives that API.
- `unjudged`: the harness cannot tell. For `dump` the output is read back by two reference
  parsers, and when they disagree with each other (or only one accepts it) the cell is
  `unjudged` with both readings, not `differs`. It is the harness's problem, not a verdict
  on the library under test, and it is listed as a known deviation until resolved.
- `error`: the harness could not measure it (the process died or stalled). The driver
  restarts the harness past that cell.

Cells that are not `ok` never carry a time or a memory figure; the result validator and
the harness's own tests enforce that. `bench/<language>/known.json` lists every cell that
is a deviation. A new deviation, or an entry that no longer holds, fails the run, and so
does an entry that names a library, task or workload that does not exist (only a real cell
that this run did not measure, in another tier or outside a filter, is left alone). The list
is checked before results and report are written; if it does not match, the report is
headed as a smoke result with that reason.

## Publishable runs

Every result records the date, versions of every library and of the harness, the compiler
or runtime, build flags, the CPU, the operating system, the governor, and the host load.
While a run is in progress the driver samples how many cores are busy with work other than
the harness. A run is labelled **publishable** only if:

- it covers everything the harnesses of its languages offer: every library, task and workload.
  A run narrowed with `--library`, `--task` or `--workload` is never publishable, because the
  strongest competitor or a losing workload may be the one left out; the reason names what
  was left out. `--lang` is different: a run of one language is complete for that language,
  so a Go-only run that includes all of Go's libraries, tasks and workloads can be published;
- it is a full run that finished every cell it set out to measure, with no cell that crashed
  or stalled (`error`);
- the harness was pinned to one CPU (`--pin`);
- the CPU frequency governor is `performance` (anything else, `powersave` included, lets the
  clock change during the run, and an unreadable governor counts the same);
- the list of known deviations matched the run;
- other work kept at most 1.0 cores busy for 95% of the samples and at most 4.0 at the
  worst sample;
- the one-minute load average was at most 3.0 at the start and at the end (the harness's
  own core included);
- at least five load samples were taken.

Otherwise the report is headed **SMOKE RESULT, NOT PUBLISHABLE** and lists the reasons.
The thresholds are in `PUBLISH_RULE` in `bench/oneyaml_bench/results.py` and are recorded
in each result, together with the governor and the CPU the harness was pinned to. Isolated
cores and boost are recorded (`boost`) but not checked. A host that cannot be pinned or set
to `performance` cannot produce a publishable run, and the report says which of the reasons
applies.

## Result format

`results.json`, format `oneyaml-kit-bench/1`:

```json
{
 "format": "oneyaml-kit-bench/1",
 "run": {"date": "...", "mode": "quick", "seconds": 31.0, "restarts": 0, "counters": false,
         "environment": {"cpu": "...", "cpus": 32, "os": "...", "governor": "...", "...": "..."},
         "publishability": {"publishable": false, "label": "smoke", "reasons": ["..."], "rule": {}, "measured": {}},
         "selection": {"languages": "all", "libraries": "all", "tasks": "all", "workloads": "all",
                       "left_out": {"libraries": [], "tasks": [], "workloads": []}},
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
   "memory": {"method": "counting global allocator", "peak_bytes": 51000,
              "allocated_bytes": 134000, "allocations": 1200},
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
the workloads with their sources and licences. The ratio is the same everywhere: our value
divided by the best competitor's, so below 1.0× is better for us, in the tables, the charts and
the alt text. Charts are bars that grow from the 1.0× (equal) line on a log scale from 1/8× to
8×, so twice as fast and twice as slow are drawn equally long on opposite sides, left better
and right a loss; a linear axis would squeeze one of them. A bar beyond the axis is clipped and
marked ▸ or ◂. The charts come in a light and a dark variant and their alt text carries every
number. The report passes the same public-text check as hand-written
text.

## README and docs blocks

`bin/bench readme` writes generated blocks into a library's README and docs from the same
result files as the evidence page:

```
bin/bench readme --results out --library oneyaml-go --readme ../oneyaml-go/README.md \
    --docs ../oneyaml-go/docs [--charts docs/img] [--allow-smoke]
```

`--results` is a directory holding the benchmark result (`bench/results.json` or `results.json`)
and the library's conformance result (`conformance/<library>.json`, `results/<library>.json` or
`<library>.json`); `--bench` and `--conformance` name the files directly. `--charts` is relative to
the README. The files looked at are the README, `<docs>/performance.md` and
`<docs>/conformance.md`; the README must hold at least one block, the two docs files are skipped
when missing or without blocks.

A block is the text between a begin marker that names its kind and an end marker:

```
<!-- generated:bench-hero -->
<!-- end generated -->
```

| Kind | Content |
|---|---|
| `bench-hero` | Chart (light and dark SVG; a row and alt text for every task), one row per task, method strip |
| `bench-full` | Every task × every workload, every competitor as a column, time and memory, method strip |
| `conformance-line` | `yaml-test-suite <release>: n/total` loose and strict, the kit's own cases, link to `conformance.md` |
| `conformance-deviations` | Every known deviation of the library with its reason |

A file may hold several blocks. An unknown kind, a nested, extra, missing or repeated marker
is an error: the command exits non-zero and writes nothing, not even the charts, in any file.
Bytes outside the blocks, including line endings, are kept, and a second run on the same inputs
leaves every file byte-identical, so a docs-sync test can run it and compare.

**The hero row.** A row aggregates the real-world workloads the task has (all of its workloads
when it has none, and the block says so). For each workload the ratio is ours ÷ that workload's
strongest competitor, as everywhere else; the row shows the geometric mean of our times and of
those competitors' times, whose ratio equals the geometric mean of the per-workload ratios. Where
several competitors were strongest, the column names them all with how often, and the time next to
them is a geometric mean across them. A workload where our cell is `differs`, `rejects`, `error` or
`unsupported` is listed in the row and the row is marked partial; a row with no workload that has
a number is `NOT COMPARABLE`. A task that competitors have cells for and our library has none for
is listed as not run. Nothing is dropped silently.

The verdict is never better than the per-workload verdicts allow: a row with a losing workload is
marked mixed or loss, and a row with no winning workload is never marked a win. The mark comes from
the kit's verdict on each workload (its noise floor, and for memory its observed ranges and the
tie it gives when fewer processes measured than declared), not from the mean. Time and memory each
show a count of better, level and worse workloads, and the memory cell names the competitor(s)
that had the smallest peak, which need not be the time winner. The chart has a row for every
task, in the same colour and words as the table: `mixed` is amber, a partial task says which
workload has no number, and a task without a number is a labelled row with no bar. The alt text
counts tasks and gives each task's ratio, mark, workload counts and gaps. A † names a competitor
that did less than the full task.

The method strip gives the date, the kit version and revision, library and competitor versions,
runtime, flags, warm-up, sample count and statistic (each time is a median), the memory method,
CPU, OS, governor and host load, and the run's publishability label. A benchmark result records
the kit revision but not its version, so the version is read from the conformance result when
there is one, and otherwise from the checkout that renders the block, and the block says so.

A result that is not publishable is refused unless `--allow-smoke` is given, because a README is
public and a smoke number is easily quoted as a claim; with the flag every block opens with a
"SMOKE RESULT, NOT PUBLISHABLE" notice and the reasons.

**Conformance blocks** are refused (exit 1, nothing written) unless the result is for the library,
comes from a complete run (`run.complete`, so not a filtered one), has no unlisted, stale or unknown
deviation (`deviations.ok`), is at the pinned yaml-test-suite release, and holds every case of the
pinned set. The line gives the loose and strict counts, the counts against the corrected
expectations where the result carries them, and the kit's own cases. It says whether those are the
fast set (slow cases not run, with the numbers) or all of them.

Generated text and charts pass the kit's public-text check or nothing is written. Files are written
beside their targets and renamed over them, so a failed write leaves each file whole, old or new.
The layout of each block is in the `render_*` functions of `bench/oneyaml_bench/readme.py`.

## Adding a library or a language

See [benchmark-adapters.md](benchmark-adapters.md).
