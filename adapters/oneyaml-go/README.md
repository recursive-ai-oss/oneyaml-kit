# oneyaml-go adapter

The kit adapter for `oneyaml.dev/go`. Unlike the other Go adapters it
does not pin a published version: the kit does not fetch the library, it builds it from a
checkout on the machine.

## Pointing it at a checkout

```sh
export KIT_ONEYAML_GO_SRC=/path/to/oneyaml-go    # any directory with the library's go.mod
bin/kit fast --adapter oneyaml-go
bin/bench run --mode quick --lang go             # the harness links the same checkout
```

The variable can name a clone, a snapshot or a worktree. `build.sh` writes a module file into the
build output directory (`KIT_GO_BIN`) that requires the library and replaces it with that
directory, and builds with `-modfile`, so no tracked `go.mod` changes. The version in the report
is the checkout's commit (`devel` when it is not a git checkout).

## Without the variable

The adapter is **unavailable**, not failed (`requires_env` in `adapter.json`). `bin/kit fast`
prints `oneyaml-go: SKIPPED, not available: environment variable KIT_ONEYAML_GO_SRC is not set`,
the report lists it under "Not run", `bin/gate` stays green and the harness builds without the
library. The module still compiles without the checkout (`unlinked.go`), so formatting, vet and
tests run everywhere.

## What it declares

No capability yet, so every case is unsupported and the deviation list is empty. `main.go` lists
the three operations (events, value, patch) as seams: what each will call, and which
capabilities to declare with it.

The benchmark side is `bench/go/libs_own.go`, built in only with the same variable; it takes part
in no task yet.

The general rules for Go adapters are in [../gokit/README.md](../gokit/README.md).
