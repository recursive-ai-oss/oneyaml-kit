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

## Naming the library revision

A result must say which commit of the library it measured. For the Go and C# libraries, built from a
checkout named by `KIT_ONEYAML_GO_SRC` or `KIT_ONEYAML_CSHARP_SRC`, one rule
(`tools/library-rev.sh`, used by every build script and by the benchmark driver) names it:

| Source | Recorded revision |
|---|---|
| a git checkout | its short commit; `<commit>+modified` when tracked files have uncommitted changes, when there are untracked files that git does not ignore, or when git cannot say |
| a tree with no git history of its own, such as an export, and `KIT_ONEYAML_<LANG>_REV` set (`GO` or `CSHARP`) | the value as given (7 to 40 hexadecimal characters), marked as supplied rather than read from git |
| the same, without the variable | `devel`, marked as unknown |

On a git checkout `KIT_ONEYAML_<LANG>_REV` is only a check: it must be at least 7 characters, a prefix of
the checkout's commit, and the tree must be clean; otherwise the build and the run refuse. `devel` and
anything containing `+modified` are refused as a supplied value. A supplied value is also refused when the source holds files that another git checkout tracks: the revision would then describe that repository, not the library. A directory the enclosing checkout ignores or does not track counts as having no history.

`devel` and `+modified` are not revisions anyone can check out, so they are never publishable:

- `bin/bench run --mode full` refuses to start when any of these libraries would be recorded that way
  and no override is given. The message names the library and the revision. Build from a clean checkout;
  for a source with no git history, name its commit as `KIT_ONEYAML_GO_REV=<commit>` (the same for
  `CSHARP`); or add `--smoke`, which labels the result a smoke result. A full run also refuses when the
  built library reports a different revision from the one read before the build.
- Quick and smoke runs work as before. Their report says the revision is unknown, was supplied, or had
  uncommitted changes, and `results.json` holds it under `run.library_revisions`.
- The conformance report shows the stamped version as `(revision unknown)` for `devel` and as
  `<commit>+modified (uncommitted changes)`, never as a version number. `bin/kit` does not refuse,
  because its results are not benchmark claims; a supplied revision appears there as given.

The Rust adapter and harness build the published crate at an exact locked version, so they have no
revision to name.

## Output and exit status

`bin/kit fast` writes `out/results/<adapter>.json` (the [result format](result-format.md)) and
`out/conformance.md`, the report generated from them. `$KIT_OUT` moves the directory.

It exits non-zero when a library fails a case that its deviation list does not name, or when a
list entry has gone stale. The deviation lists are described in the
[adapter protocol](adapter-protocol.md#known-deviations).
