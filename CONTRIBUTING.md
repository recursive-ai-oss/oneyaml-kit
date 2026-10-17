# Contributing to oneyaml-kit

Thank you for helping. New cases, corrections to expectations, adapters for other YAML libraries,
benchmark harnesses and documentation fixes are all welcome.

## Before you start

- **Found a case whose expectation is wrong?** Open an issue with the case id and the passage of
  the YAML 1.2.2 specification or the RFC that says otherwise. Expectations come from the
  specification, never from what a library prints ([principles](docs/principles.md)).
- **Want to add a library?** Read the [adapter protocol](docs/adapter-protocol.md#writing-an-adapter)
  for conformance, or the [benchmark adapters](docs/benchmark-adapters.md) page for the benchmark.

## Set up

You need Python 3.10 or newer and `git`; the kit itself uses the standard library only. Each area
you touch needs its own toolchain (Rust, Go or the .NET SDK), as described in
[running the kit](docs/running.md#what-you-need); a change to the kit's code or cases also runs
the Rust reference adapter.

```sh
git clone https://github.com/recursive-ai-oss/oneyaml-kit
cd oneyaml-kit
bin/gate
```

## Repository layout

| Path | Contents |
|---|---|
| `suites/yaml-test-suite/` | The yaml-test-suite release `data-2022-01-17`, vendored untouched, with its source record, licence and our corrections. |
| `suites/own/cases/` | The kit's own cases: parse, schema, edits by patch, diagnostics, safety. |
| `adapters/` | One directory per library under test: manifest, adapter program, known deviations. |
| `tools/oneyaml_kit/` | Loader, runner, comparison, report generator (Python standard library only). |
| `bench/` | The benchmark harness: workloads, the Rust, Go and .NET harnesses, the driver and the report generator. See [benchmarks](docs/benchmarks.md). |
| `tests/` | The kit's own tests. |
| `docs/` | The documentation linked from the [README](README.md#where-to-go). |

## Making a change

- **A case:** follow the [case format](docs/case-format.md). Cases are raw bytes: don't open them
  in an editor that rewrites line endings, and don't let a formatter touch `suites/`. Then update
  the [pins](docs/case-format.md#pins) with `bin/kit pins --write` and check their diff.
- **An adapter:** follow [writing an adapter](docs/adapter-protocol.md#writing-an-adapter), and
  list each genuine failure in its `deviations.json` with a one-sentence reason.
- **A benchmark library or language:** follow the [benchmark adapters](docs/benchmark-adapters.md)
  contract.

## Check

Run `bin/gate` before you hand over a change. It runs the kit core and the areas your change
touched; CI runs the full gate on every push and pull request. [The gate](docs/gate.md) says what
each tier runs.

The gate also runs `bin/kit public-text`, which refuses internal process text in the
repository's files.

## Licence of contributions

Unless you say otherwise, a contribution to the kit's code is dual licensed under MIT or Apache-2.0, and a contribution to the kit's own cases (`suites/own/`) is dedicated to the public domain under CC0 1.0, the same as the rest of each (see the README's Licence section).
