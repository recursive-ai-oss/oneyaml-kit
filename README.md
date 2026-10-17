# oneyaml-kit

**A language-neutral YAML 1.2 conformance suite and benchmark harness.** Connect your YAML
library with one small adapter, and the kit shows you, case by case, where it departs from the
specification and how it compares with the strongest competitor in its language.

[![gate](https://github.com/recursive-ai-oss/oneyaml-kit/actions/workflows/gate.yml/badge.svg)](https://github.com/recursive-ai-oss/oneyaml-kit/actions/workflows/gate.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](docs/running.md#what-you-need)
[![yaml-test-suite data-2022-01-17](https://img.shields.io/badge/yaml--test--suite-data--2022--01--17-blue.svg)](suites/yaml-test-suite/SOURCE.md)
[![Licence: MIT OR Apache-2.0, cases CC0](https://img.shields.io/badge/licence-MIT%20OR%20Apache--2.0%2C%20cases%20CC0-blue.svg)](#licence)

## What it checks

- **Spec conformance.** Every case of the yaml-test-suite, with corrections scored beside the
  upstream expectation where the suite is wrong, plus the kit's own parse cases
  ([case format](docs/case-format.md#upstream-cases-and-corrections)).
- **Schema.** What each scalar resolves to under the YAML 1.2 Core schema, compared exactly:
  `1` and `1.0` are different values ([kit values](docs/case-format.md#kit-values)).
- **Edits and patches.** RFC 6902 and RFC 7396 patches with the exact bytes expected afterwards:
  only the edited value changes, and comments stay ([edit semantics](docs/edit-semantics.md)).
- **Safety.** Hostile inputs with time and memory bounds that the runner enforces
  ([bounded cases](docs/case-format.md#bounded-outcomes-safety)).
- **Diagnostics.** The error category, line and column a library reports
  ([expectations](docs/case-format.md#expectations)).
- **Speed and memory.** Each language's own benchmark tool, every answer checked before it is
  timed, against the strongest competitor on each task ([benchmarks](docs/benchmarks.md)).

## Quick start

You need Python 3.10 or newer, and a Rust toolchain for the adapter this example runs.

```sh
git clone https://github.com/recursive-ai-oss/oneyaml-kit
cd oneyaml-kit
bin/kit fast --adapter oneyaml-rust   # build the adapter and run every fast case
bin/bench run --lang rust             # a quick benchmark run of the Rust libraries
```

The first command writes `out/results/oneyaml-rust.json` and the report `out/conformance.md`;
the second writes `out/bench/report.md` and its charts. To put your own library through the
kit, add a directory under `adapters/` with a manifest and a program that speaks the
[adapter protocol](docs/adapter-protocol.md), then run `bin/kit fast --adapter <name>`.

## Where to go

- **Running the kit:** [commands, output and exit status](docs/running.md).
- **Writing cases:** the [case format](docs/case-format.md) and the
  [edit semantics](docs/edit-semantics.md) the edit cases follow.
- **Connecting a library:** the [adapter protocol](docs/adapter-protocol.md) and the
  [result format](docs/result-format.md) it produces.
- **Benchmarks:** [how they are measured and reported](docs/benchmarks.md), and the
  [benchmark adapters](docs/benchmark-adapters.md) contract for adding a library or a language.
- **How the kit judges:** [principles](docs/principles.md).
- **Contributing and security:** [CONTRIBUTING.md](CONTRIBUTING.md), [the gate](docs/gate.md)
  that checks every change, [SECURITY.md](SECURITY.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- **The oneyaml family:** [oneyaml-rust](https://github.com/recursive-ai-oss/oneyaml-rust),
  [oneyaml-go](https://github.com/recursive-ai-oss/oneyaml-go) and
  [oneyaml-csharp](https://github.com/recursive-ai-oss/oneyaml-csharp), each checked and
  measured by this kit.

## Licence

- **Kit code and docs** (`tools/`, `bin/`, `adapters/`, `bench/`, `tests/`, `docs/`, `.github/`):
  [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE), at your option. This covers every file
  under `bench/` except the vendored workloads.
- **The kit's own cases** (`suites/own/`): [CC0 1.0](suites/own/LICENSE), so other YAML projects
  can copy them without attribution. A `licence` field in a case's provenance names the licence
  of the source the case was derived from; the case data itself is CC0.
- **Vendored material keeps its own licence:** the yaml-test-suite data in `suites/yaml-test-suite/`
  is MIT (see its `LICENSE`; `SOURCE.md` records where it came from), and each real-world
  benchmark file in `bench/workloads/real/` names its repository, revision and licence in its
  `workload.json` and in `SOURCES.md`, with the licence texts in `LICENSES/`. In
  `suites/yaml-test-suite/`, `corrections.json` and `SOURCE.md` are the kit's own text and are
  CC0 like the own cases.
