# Benchmarks

The benchmark measures YAML libraries, ours and their competitors, on the same inputs and the
same tasks, and generates the tables and charts that library READMEs embed. Each language uses
its own standard benchmark tool, every answer is checked before it is timed, and
a quick run says plainly that it is only a smoke result.

```sh
bin/bench run                    # quick mode: seconds to a minute, writes out/bench/
bin/bench run --mode full        # full mode: each tool's documented short setting, for a quiet machine
bin/bench run --lang go --task dump --workload gha-workflow   # a slice of either
bin/bench report out/bench                                    # re-render a report from results.json or results-*.json
bin/bench workloads              # write the workload set only
bin/bench readme --results out --library oneyaml-go --readme README.md --docs docs   # see below
bin/bench-cloud quick|full [--lang go]   # one run on a rented machine, see below
```

Output goes to `$KIT_OUT/bench` (default `./out/bench`, never committed): `results.json`,
`report.md`, `charts/*.svg`, and the prepared `workloads/`. Build output goes to
`$CARGO_TARGET_DIR`, or to `out/bench/target`.

Quick mode runs each tool with a very short setting. Full mode adds the large scaling inputs and
runs each cell for about as long as a person would wait for one benchmark:

| Language | Tool | Full mode runs each cell with |
|---|---|---|
| Go | `testing.B` with allocation reporting | the standard `-benchtime` of 1 s |
| Rust | criterion | a 1 s warm-up, 2 s of measurement and its smallest sample size, 10 |
| C# | BenchmarkDotNet | its `ShortRun` job: 3 warm-up and 3 measured iterations, in process |

The tools' own defaults (criterion's 3 s, 5 s and 100 samples; BenchmarkDotNet's default job, which
iterates until the spread settles) took tens of seconds to minutes per cell on the large inputs, so a
whole run did not finish. A cell now takes a few seconds in full mode, plus the time to prepare its
input on the large ones.

`bin/bench run` prints one line per finished cell (language, library, task, workload, status and
the seconds it took) and a line as each language starts and ends. It writes `results-<language>.json`
as soon as a language finishes and `results.json` at the end, so a run that is stopped early keeps
every language that completed. `bin/bench report out/bench` (a directory) renders whichever languages
are present and marks the report **INCOMPLETE RUN**, naming the languages it lacks. A run limited with
`--lang` is complete when it finishes the languages it was asked for; one stopped before a requested
language finished is incomplete, and a README block made from it carries the same notice. A quick run, or any run outside the cloud machine (or one started with `--smoke`, or from `bin/bench-cloud --here`), is a
**smoke result** and its report says so.

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
is released after the clock stops. The one exception is the Go `unchanged-emit` task: writing out an unchanged
document can take nanoseconds, so the harness shares one parsed document between runs, but only for a library
whose write of a used document allocates the same as a write of a fresh one (checked before timing, within 2%).
A library that does less the second time gets a fresh document for every run. The result of every iteration (a value tree, an output
buffer) is dropped inside the clock, as a caller's would be. Edit paths are resolved from the
workload's pointers once, before the clock, into the library's own path syntax.

`unchanged-emit` is timed through the library's real write API, and a lossless library that returns the text an unchanged document holds is not marked as doing different work for it. Its time can be far below that of a library that re-serialises a tree. That is the work the task measures, the user's write path for a document they did not edit, and not a different task.

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
  `dump` and `document-parse`. Only the plain outcome is `spec`. A `bytes-only` result is a
  document judged on its bytes because the library gives no value to compare. A third, `value-only`, exists for one purpose: the
  baseline patcher that works on JSON and cannot keep the text (see
  [benchmark-adapters.md](benchmark-adapters.md)); its output is judged on the value it holds.
  A fourth, `harness-rendered`, marks an edit whose lookup and new text are written by the harness
  because the library only replaces a range of characters; the work is timed.

### Metrics

Each language reports what its standard tool reports, and the report names the tool:

| Language | Time | Memory |
|---|---|---|
| Rust | criterion, mean time of one run | bytes and calls requested in one run, counted by a global allocator |
| Go | `testing.Benchmark`, ns/op | B/op and allocs/op from `ReportAllocs` |
| .NET | BenchmarkDotNet's default job, run in process, mean time per task | bytes allocated per task from `MemoryDiagnoser` |

The starting state of every run is built outside the clock and released after it. Quick mode only
shortens the tool's own time settings. Figures from different tools are never mixed in one table.

### Verdicts

For each row the reference is the **strongest competitor in that row on that metric**: the fastest
for time, the least allocated for memory, whatever it did. Our library's ratio is its value divided
by the reference's, so below 1.0 is better. A gap is a **tie** when it is under 5% for time or 2% for
memory; otherwise it is a **win** or a **loss**. Losses are marked in the tables and the charts.

One library is measured with a wider memory band (`MEMORY_BANDS` in `bench/oneyaml_bench/results.py`).
In repeated quick-mode runs, YamlDotNet's generic load allocated one of a few distinct amounts per run,
about 5.6% apart and up to 11.7% apart in total (helm-values-64k, openapi-100k, records-64k). Every
other .NET library varied by under 1.2%. The cause is not established. A memory comparison uses the
wider band of the two libraries compared, so a gap under 12% against YamlDotNet is a tie, and every
other comparison keeps 2%. Go and Rust allocation counts repeat closely between runs, so they keep 2%.

A cell judged more loosely (`bytes-only`, `value-only`, `harness-rendered`) or from a library that
takes and returns text (`state: the text`) is shown with a † and is never called the best. It is still
compared with: when it is faster than ours, or is the only competitor, the verdict names it with its
mark ("loss vs yaml-edit †, bytes-only") and a loss to it is a loss in the tally and the weakest row.
A row has "no rival" only when no competitor answered correctly. `merged` carries no mark and may be
called the best on every task where it occurs (`generic-load`, `dump` and `document-parse`), and is
labelled wherever it appears. The rule and the reasons are in
[benchmark-adapters.md](benchmark-adapters.md#what-may-be-the-best).

### A language without a library of ours

The verdicts compare our library with the strongest competitor. A language whose harness has no
library of ours has no such comparison; its tables name the best library in each row instead (a
faster † cell is named beside it, marked).

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
  on the library under test.
- `error`: the harness could not measure it (the process died). The driver
  restarts the harness past that cell.

Cells that are not `ok` never carry a time or a memory figure; the result validator and the
harnesses' own tests enforce that. A cell that is a `differs`, `rejects`, `unjudged` or `error` is
listed with its reason in the report's table of cells without a number.

## Smoke results

Every result records the date, the versions of every library and of the harness, the tool, the
compiler or runtime, the CPU, the operating system and the host load before and after. A quick run
is always labelled **SMOKE RESULT**; A full run anywhere but the cloud machine
(`KIT_INSTANCE_TYPE` unset), or one shipped with `--here`, is labelled the same way. `bin/bench readme` refuses a smoke result unless `--allow-smoke` is
given, because a README is public and a smoke number is easily quoted as a claim.

## Running on a rented machine

`bin/bench-cloud quick` or `bin/bench-cloud full` runs the whole benchmark on one on-demand cloud
instance and brings the results back. It launches one instance of the type set in the config from a
stock Ubuntu image. The instance's start-up script clones the public kit and library repositories at
the revisions given, installs the toolchains, runs the benchmark and uploads one results tarball to a
presigned S3 URL (`--lang` limits the run and can be repeated). If the deadline passes first, the
tarball still holds the languages that finished and the log. The script waits for the tarball, downloads it, writes the report and terminates the
instance. The instance has no SSH, no inbound ports and no credentials, and shuts itself down
(terminating) after a deadline whatever happens. The configuration file is named by `KIT_CLOUD_CONFIG`
(default `~/.config/oneyaml-kit/cloud.env`); see the head of `bin/bench-cloud` for its variables.

## Result format

`results.json`, format `oneyaml-kit-bench/2`:

```json
{
 "format": "oneyaml-kit-bench/2",
 "run": {"date": "...", "mode": "quick", "smoke": true, "seconds": 31.0,
         "environment": {"cpu": "...", "cpus": 32, "os": "...", "kit_revision": "..."},
         "load": {"before": 1.8, "after": 2.2}, "expected_languages": ["csharp", "go", "rust"]},
 "harnesses": {"rust": {"name": "oneyaml-bench-rust", "version": "0.2.0", "runtime": "rustc ...", "tool": "criterion",
                        "timer": "...", "memory": "...", "libraries": [{"id": "oneyaml", "package": "oneyaml",
                        "version": "1.1.0", "role": "own", "settings": {}}]}},
 "workloads": [{"id": "gha-workflow", "kind": "real-world", "input_bytes": 9656, "source": {}, "tasks": []}],
 "cells": [
  {"language": "rust", "library": "oneyaml", "role": "own", "task": "generic-load", "workload": "gha-workflow",
   "status": "ok", "variant": "spec", "input_bytes": 9656,
   "time": {"ns": 149000.0, "stat": "mean"},
   "memory": {"allocated_bytes": 134000, "allocations": 1200}},
  {"language": "rust", "library": "yaml-edit", "role": "competitor", "task": "edit-script",
   "workload": "reference-b", "status": "differs", "detail": "byte 26: got ..., expected ..."}
 ]
}
```

`run.expected_languages` lists the languages the run was asked to measure (every language with a
harness when `--lang` is not given); a language in it with no
entry in `harnesses` is missing, and the run is incomplete. `results-<language>.json` files have the
same format with one language each.

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
the kit's verdict on each workload (its tie band), not from the mean. Time and memory each
show a count of better, level and worse workloads, and the memory cell names the competitor(s)
that allocated least, which need not be the time winner. The chart has a row for every
task, in the same colour and words as the table: `mixed` is amber, a partial task says which
workload has no number, and a task without a number is a labelled row with no bar. The alt text
counts tasks and gives each task's ratio, mark, workload counts and gaps. A † names a competitor
that did less than the full task.

The method strip gives the date, the kit version and revision, library and competitor versions,
runtime, the tool and its statistic (a mean), the memory method, CPU, OS and host load, and
whether the run is a smoke result. A benchmark result records
the kit revision but not its version, so the version is read from the conformance result when
there is one, and otherwise from the checkout that renders the block, and the block says so.

A smoke result is refused unless `--allow-smoke` is given, because a README is public and a smoke
number is easily quoted as a claim; with the flag every block opens with a "SMOKE RESULT" notice.

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
