# oneyaml-kit

A versioned, language-neutral YAML 1.2 test suite and conformance runner. It defines
expected behaviour as data (events, values, exact output bytes, error categories and
positions), runs any library through a small adapter protocol, and generates reports
from the results.

## What is in it

| Path | Contents |
|---|---|
| `suites/yaml-test-suite/` | The yaml-test-suite release `data-2022-01-17`, vendored untouched, with its source record, licence and our corrections. |
| `suites/own/cases/` | The kit's own cases: parse, schema, edits by patch, diagnostics, safety. |
| `adapters/` | One directory per library under test: manifest, adapter program, known deviations. |
| `tools/oneyaml_kit/` | Loader, runner, comparison, report generator (Python standard library only). |
| `bench/` | The benchmark harness: workloads, the Rust, Go and .NET harnesses, the driver and the report generator. See [docs/benchmarks.md](docs/benchmarks.md). |
| `tests/` | The kit's own tests. |
| `docs/` | [Case format](docs/case-format.md), [adapter protocol](docs/adapter-protocol.md), [result format](docs/result-format.md), [edit semantics](docs/edit-semantics.md), [benchmarks](docs/benchmarks.md), [benchmark adapters](docs/benchmark-adapters.md). |

## Running it

Python 3.10 or newer and each adapter's own toolchain (the oneyaml-rust adapter needs
a Rust toolchain; the Go adapters, described in [adapters/gokit](adapters/gokit/README.md),
need Go).

```sh
bin/kit fast                      # every adapter, whole fast set, writes out/
bin/kit fast --adapter oneyaml-rust --suite own
KIT_ONEYAML_GO_SRC=/path/to/oneyaml-go bin/kit fast --adapter oneyaml-go   # the Go library, from a checkout
bin/kit report out/results/*.json # render a report from result files
bin/kit list                      # case counts
bin/kit coverage                  # which YAML 1.2.2 productions the own cases name (names, not coverage)
bin/gate                          # hand-over gate: the kit core and the areas your change touched
bin/gate --full                   # everything, every adapter (CI runs this)
bin/gate --plan                   # show what would run
bin/gate --areas bench-go         # named areas instead of choosing by changes
bin/bench run                     # benchmarks, quick mode: results.json, report.md and charts in out/bench/
bin/bench run --mode full         # full mode; labelled publishable only on a quiet host
```

`bin/kit fast` writes `out/results/<adapter>.json` and `out/conformance.md`
(`$KIT_OUT` moves the directory) and exits non-zero when a library fails a case that
its deviation list does not name, or when a list entry has gone stale.

The gate has two tiers. The hand-over gate (`bin/gate`) always runs the kit core (corpus
pins, the kit's own tests, the public-text check) and adds an area only when the files that
differ from `main` touch it, and then only that area's quick steps: format and lint checks,
unit and self-tests, and for a benchmark harness a smoke run over three small workloads. Areas
are the adapter languages (`rust`, `go`, `csharp`, ...), `bench` (the benchmark driver, report
and workloads), one benchmark area per harness language (`bench-rust`, `bench-go`,
`bench-csharp`, so a change to one harness never builds another) and `core`. A change to the
kit's code or cases also runs the reference adapter, and a change to the benchmark driver also
smoke-tests the Rust harness. Areas run side by side, four at a time unless `--jobs` says
otherwise. The hand-over gate has a time budget of 150 seconds (`--budget`) and fails when a run
exceeds it, which keeps the tier inside its three-minute limit; the fix is a faster step or
moving it to the full tier. The full gate (`bin/gate --full`) runs every area and every
step, including the slow ones (the end-to-end benchmark pipeline tests, the quick benchmark
run over every workload, the counter check, the plain-build lint of the Rust harness), and it
has no budget. CI runs it as one job per area (`--areas <area>`, each on its own runner with
its own caches), so the slowest area sets the time. A bounded case's time limit is stretched
on a slow or busy machine (see the case format), so a verdict does not depend on how fast
the machine is. Each step
logs under `out/gate/` and shows its output only when it fails. Before any step the gate
prints, and `out/gate/summary.json` records, the revision it checked: HEAD, the base and merge
base, a fingerprint of the change against the merge base (the binary diff plus the path and
content hash of every untracked file) and whether the tree is dirty, so two runs can be
compared. Renames count as a deletion and an addition, so the area a file left is checked too.

## Principles

- **Cases are data.** Inputs and expected bytes are stored raw; nothing normalises
  them.
- **Expectations come from the specification**, YAML 1.2.2 and the RFCs, never from a
  library's output. Where the upstream suite is wrong, our expectation, the reason and
  the citation are recorded beside it and both are scored.
- **Comparison is full.** Every byte, the whole event stream, the whole value. A
  narrower view is a named mode.
- **Results are per capability, strict and loose.** A case an adapter cannot do is counted
  as unsupported, separately from passes and failures.
- **Safety cases name the acceptable outcomes** (an exact result, a limit error, or both)
  and carry their own time and memory bounds, which the runner enforces and records.
- **Behaviour an adapter supplies itself is declared and marked.** Passes that depend on
  it are counted in the Loose and Strict columns and again in their own Emulated column,
  so a reader can subtract them.
- **Known deviations are listed per library and verified.** A new failure, or an entry
  that no longer holds, fails the run.
- **Reports are generated** from result files.
