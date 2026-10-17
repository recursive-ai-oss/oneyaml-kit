# Running the kit

How to run libraries through the kit's cases, what it writes and when it fails. For the
benchmark, see [benchmarks](benchmarks.md); for the kit's own checks, see [the gate](gate.md).

## What you need

- Python 3.10 or newer. The kit itself uses the standard library only.
- Each adapter's own toolchain: the oneyaml-rust adapter needs a Rust toolchain; the Go adapters,
  described in [adapters/gokit](../adapters/gokit/README.md), need Go; the .NET adapters, described in
  [adapters/dotnet-common](../adapters/dotnet-common/README.md), need the .NET SDK.

## Commands

```sh
bin/kit fast                                     # every adapter, the whole fast set, writes out/
bin/kit fast --adapter oneyaml-rust --suite own  # one adapter, the kit's own cases only
KIT_ONEYAML_GO_SRC=/path/to/oneyaml-go bin/kit fast --adapter oneyaml-go              # the Go library, from a checkout
KIT_ONEYAML_CSHARP_SRC=/path/to/oneyaml-csharp bin/kit fast --adapter oneyaml-csharp  # the C# library, likewise
bin/kit report out/results/*.json                # render a report from result files
bin/kit list                                     # case counts by suite, category and operation
bin/kit coverage                                 # which YAML 1.2.2 productions the own cases name (names, not coverage)
```

`bin/kit fast --help` lists the other options: `--category`, `--case`, `--timeout`, `--slow`
(include cases tagged slow) and `--no-build`.

The oneyaml-go and oneyaml-csharp adapters build the library from a checkout named by their
variable; without it they are skipped, not failed. Their READMEs say more:
[oneyaml-go](../adapters/oneyaml-go/README.md), [oneyaml-csharp](../adapters/oneyaml-csharp/README.md).

## Output and exit status

`bin/kit fast` writes `out/results/<adapter>.json` (the [result format](result-format.md)) and
`out/conformance.md`, the report generated from them. `$KIT_OUT` moves the directory.

It exits non-zero when a library fails a case that its deviation list does not name, or when a
list entry has gone stale. The deviation lists are described in the
[adapter protocol](adapter-protocol.md#known-deviations).
