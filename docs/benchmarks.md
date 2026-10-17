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
language finished is incomplete, and a README block made from it carries the same notice. A quick run, or one started with `--smoke`, is a
**smoke result** and its report says so. A full run is a measurement wherever it ran, on the cloud machine or on a local one; the result records which.

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
| `patch` | .NET: the library's parsed document (or, for a baseline that keeps no text, its loaded data tree). Go: the input bytes. Rust: the same as .NET, but no library there has a patch engine yet, so the task has no cells | .NET: apply an RFC 6902 patch atomically, write the bytes. Go: every library's patch is timed from text to text, so the parse and the write are inside the clock (the harness records this as `text_tasks`) | the bytes equal the expected bytes, and a second patch with a failing step leaves the document unchanged and reports an error |

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

None of these is a peak: each is the memory one operation asked for, so the memory columns are
headed by what the language reports (`Allocated bytes per operation (B/op)` for Go,
`Allocated bytes per task (MemoryDiagnoser)` for .NET, `Requested bytes per run (allocator count)`
for Rust) and never "peak memory". The settings a run used (Go's `-benchtime`, criterion's warm-up
and measurement time, BenchmarkDotNet's job) are recorded in the harness description of that run
and printed as recorded. A result from before the harnesses recorded the settings of their own
mode is printed without the clause that describes the other mode; nothing is filled in.

The starting state of every run is built outside the clock and released after it. Quick mode only
shortens the tool's own time settings. Figures from different tools are never mixed in one table.

### Verdicts

For each row the reference is the **strongest competitor in that row on that metric**: the fastest
for time, the least allocated for memory, whatever it did. Our library's ratio is its value divided
by the reference's, so below 1.0 is better. A gap is a **tie** when it is under 5% for time or 2% for
memory; otherwise it is a **win** or a **loss**. Losses are marked in the tables and the charts.

A memory gap under 64 bytes (`MEMORY_FLOOR_BYTES`) is also a tie, whatever the ratio, when a comparison
involves one of `oneyaml-csharp`, `oneyaml-csharp-gen`, `sharpyaml-syntax` or `vyaml` (`MEMORY_FLOORS`).
On .NET cells under 1 KB a few bytes are a large ratio, and the figure itself moves between
processes: repeated quick runs of the same code differed by up to 22 bytes for our .NET library, 6 bytes
for `sharpyaml-syntax` and 3 for `vyaml` on such cells. The floor is about three times the largest of
those. Rust allocation counts repeated exactly (no spread over 35 repeated cells, small ones included)
and Go's repeat closely, so Rust and Go comparisons have no floor and a small gap there is a result.
A gap of 64 bytes or more is judged by the relative band as before, so a loss never disappears except
by this byte floor on the libraries named.

One library is measured with a wider memory band (`MEMORY_BANDS` in `bench/oneyaml_bench/results.py`).
In repeated quick-mode runs, YamlDotNet's generic load allocated one of a few distinct amounts per run,
about 5.6% apart and up to 11.7% apart in total (helm-values-64k, openapi-100k, records-64k). Every
other .NET library varied by under 1.2%. The cause is not established. A memory comparison uses the
wider band of the two libraries compared, so a gap under 12% against YamlDotNet is a tie, and every
other comparison keeps 2%. Go and Rust allocation counts repeat closely between runs, so they keep 2%.

A cell judged more loosely (`bytes-only`, `value-only`, `harness-rendered`) or from a library that
takes and returns text (`state: the text`) is shown with a † and is never called the best.
A harness can also time a whole task from text to text for every library, because the task is
defined on text there (the Go `patch` task: each library gets the input bytes and returns the
bytes). The harness names such tasks in `text_tasks` and every cell of them carries the † and the
reason "text API", ours included, so the same work has the same mark; the mark never enters a
verdict, and the report says so under the task. It is still
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
listed with the first difference the guard found in the report's table of cells without a number.

### Why a competitor's cell has no number

A competitor's `differs`, `rejects`, `unjudged` or `error` cell carries a note number, such as
`differs [3]`, in every table that shows it. The report's "Notes on competitor cells" section (and
the notes under a library's generated evidence table) gives each note once for all the cells that
share it:

- the **first difference the guard found**, taken from the result file (the path or byte, and what
  the library gave against what was expected), for the first cell of the group;
- a **short reason**, when the kit knows the behaviour behind it. Reasons are curated in
  `bench/oneyaml_bench/reasons.json`. Each names the libraries and tasks it covers and a pattern on the
  first difference, so it only explains a difference of the kind it describes, and the first
  matching entry is used. A cell with no matching entry still gets a note with its first difference.

The guard is strict on purpose: it compares every byte or value, so a library that writes a valid but
different form (`812.0` as `812`, a different indent, a dropped blank line) is marked, and the note
says what it did.

Two limits keep this honest. Only competitor cells are explained: a cell of our own library that
has no number is a plain failure with no note. And a reason that no cell of a full run uses, although
the run measured its library and task, is stale: the report ends with a "Stale reasons" section and
`bin/bench report` and `bin/bench run` exit with an error until the entry is fixed or removed. (A
run limited to some workloads can report an entry as stale that a whole run uses.) Quick runs are not
judged for stale entries.

## Smoke results

Every result records the date, the versions of every library and of the harness, the tool, the
compiler or runtime, the CPU, the logical CPU count, the memory, the operating system and the host's
1-minute load before and after, and the highest it reached while the run was going (read once per poll of
the running harness, on no thread of its own). A run on the cloud machine records its instance type; any
other run is recorded as local, and its README method line and report say so in plain words: "measured on
a shared workstation (32 logical CPUs; load 4.1 before, 5.0 after, at most 7.3 during)". A reader judges
a local number by that load. A quick run, a run started with `--smoke` and a run with `KIT_SMOKE` set
are labelled **SMOKE RESULT**. `bin/bench readme` refuses a smoke result unless `--allow-smoke` is
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
         "environment": {"cpu": "...", "cpus": 32, "where": "local", "os": "...", "kit_revision": "..."},
         "load": {"before": 1.8, "after": 2.2, "peak": 3.0}, "expected_languages": ["csharp", "go", "rust"]},
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

`run.sources` is present when a library of ours was built from a checkout at a tagged commit
(`KIT_ONEYAML_<LANG>_SRC` names the checkout): `{"go": {"library": "oneyaml-go", "revision":
"30e7fc4bf7b4", "tag": "v0.3.0"}}`. The tag is read from that checkout when the result is made. A
harness description may carry `text_tasks`, the tasks it times from text to text for every library.

## Reports

`bin/bench run` and `bin/bench report` write `report.md` and `charts/`. Everything in them
comes from the result file: the environment, the libraries and versions, one table per
task for time and one for memory with every library and "ours against the best competitor"
with its verdict, a bar chart for each, the cells without a number with their reasons, and
the workloads with their sources and licences. The ratio is the same everywhere: our value
divided by the best competitor's, so below 1.0× is better for us, in the tables, the charts and
the alt text of the report (the README hero is the one exception, see below). Charts are bars that grow from the 1.0× (equal) line on a log scale from 1/8× to
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
    --docs ../oneyaml-go/docs [--charts docs/img] [--allow-smoke] [--chart-base URL] [--library-source DIR] \
    [--chart-omit-task TASK ...]
```

**Recommended layout.** The README stays short and the evidence lives on the docs pages:

| File | Blocks |
|---|---|
| `README.md` | `bench-chart` (the chart, with a link to the performance page), and `conformance-line` if wanted |
| `docs/performance.md` | `bench-hero` (the headline table) and `bench-full` (every task, workload and competitor, time and memory) |
| `docs/conformance.md` | `conformance-line` and `conformance-deviations` |

A marker is one comment line that opens the block and the end marker that closes it; the generator
replaces what lies between them:

```
README.md:               <!-- generated:bench-chart -->
                         ...old text, replaced on each run...
                         <!-- end generated -->

docs/performance.md:     <!-- generated:bench-hero -->  ...  <!-- end generated -->
                         <!-- generated:bench-full -->  ...  <!-- end generated -->

docs/conformance.md:     <!-- generated:conformance-line -->        ...  <!-- end generated -->
                         <!-- generated:conformance-deviations --> ...  <!-- end generated -->
```

Losses always appear on the performance page: `bench-hero` and `bench-full` keep every task and
nothing can leave them out. The README chart cannot omit a losing task either (see
`--chart-omit-task` below).

`--results` is a directory holding the benchmark result (`bench/results.json` or `results.json`)
and the library's conformance result (`conformance/<library>.json`, `results/<library>.json` or
`<library>.json`); `--bench` and `--conformance` name the files directly. `--charts` is relative to
the README. The files looked at are the README, `<docs>/performance.md` and
`<docs>/conformance.md`; the README must hold at least one block, the two docs files are skipped
when missing or without blocks.

Chart links are relative to each file by default (`docs/img/<name>-dark.svg` inside a
`<picture>`), and that is the choice to make for a README: GitHub resolves them at the revision
being viewed, and package pages such as pkg.go.dev resolve them at the module's version, so a
tagged README shows the charts of its own release beside its own numbers. `--chart-base URL`
writes them as absolute URLs instead, `URL/<charts>/<name>-light.svg`, the same in every file,
for a surface that does not resolve relative paths. Pin it to a tag, never to a branch: charts read
from a branch change at the next run while the table and text stay as released. A URL with a branch
name in it (`main`, `master`, `HEAD`, `refs/heads/...`) is refused. `--library-source DIR` names a checkout of the
featured library: when the run did not record the library's release tag, the tag of the commit the
result names is looked up there and shown beside it, as `v0.3.0 (30e7fc4bf7b4)`. Only a commit with
exactly one version tag gets one; a tag is never guessed.

`--chart-omit-task TASK` (repeatable, a task id such as `unchanged-emit`) leaves a task out of the
`bench-hero` and `bench-chart` blocks: the chart, its alt text and description, and the hero table.
The `bench-full` table keeps every task. An id the result does not have is an error that lists the
ones it has. The option is for a task whose winning ratio is so large it distorts the chart: a task
whose verdict for the library is a loss or mixed, or that is not comparable because the library has
no time for it, is refused with an error naming the task and its verdict, and nothing is written.
Omitting a winning or level task works.

The hero is the headline and speaks in the family's form, higher is better: the **speed** of a row
is the strongest competitor's time divided by ours, written "N× faster", or "N× slower" for a loss,
which is never hidden; a row whose verdict is level reads "level (N× faster)". The table has six
columns: task, our time, the strongest competitor (with its version), its time, the speed, and the
verdict. Memory and the counts of better, level and worse workloads are on the full page, which
keeps ours ÷ theirs with its legend (below 1.0× is better for us), as does the evidence report. The
unchanged-emit row stays, labelled "write-back of an unedited document; not a rendering speed",
because a library that keeps the text returns it without rendering. A measured 0 B prints as `0 B`;
only the log axis of a memory chart places it at 1 B, and says so. A † marks a cell measured
differently from the task as specified; the note beside each mark says how, for our cells as well
as theirs.

The chart plots the same speed on a log axis around 1×, bars to the right faster and to the left
slower, widening to 1/64× to 64× when a bar would be clipped; each row's words sit in a column
beside the plot. Its subtitle wraps to the canvas. The image's alt text is short, because the table
follows it ("Bar chart of the table below: ..."), and the SVG's own `<desc>` holds every number and
reason. The method strip names the revision of the kit that wrote the text (`+modified` when its
files differ from the commit) beside the revision the run was measured at.

A block is the text between a begin marker that names its kind and an end marker:

```
<!-- generated:bench-hero -->
<!-- end generated -->
```

| Kind | Content |
|---|---|
| `bench-hero` | Chart (light and dark SVG, a row for every task), then a time-only table of one row per task, method strip |
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
runtime, the tool and its statistic (a mean), the memory method, CPU, OS and host load (and where it ran), and
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
