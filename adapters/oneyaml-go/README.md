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

| Capability | How it is answered |
|---|---|
| `events` | `Parse`, then a walk of the Node tree of every document with the node iterators (an explicit stack, so depth is not a limit). Tags come from `ExplicitTag`, document markers from `ExplicitStart` and `ExplicitEnd`. |
| `value` | `Decoder.Decode` into `any` for every document, converted to the kit's value notation. |
| `error-category` | `*SyntaxError.Category` (`syntax`, `encoding`) and `*LimitError` (`limit`); anything else is `other`. |
| `error-position` | `*SyntaxError` line and column, as the library reports them. |
| `patch:*`, `patch:merge` | `Document.ApplyPatch` and `ApplyMergePatch` on the document the case names. The refusal kinds map to `patch-path`, `patch-test`, `patch-invalid`, `refused-alias` (a write below an alias, or removing an anchor that aliases still use), `refused-ambiguous` and `limit`. |

Nothing is emulated: the library keeps the bytes of a refused patch itself. Not declared:
`error-events` (an error from `Parse` comes with no partial document, so the events read before
it cannot be listed) and profiles other than `core`.

## The benchmark side

`bench/go/libs_own.go` takes part in typed load, generic load, dump, document parse, unchanged
emit and edit script (`Set` and `Remove` by RFC 6901 pointer). The patch task uses `Document.Apply`. Unchanged emit runs a batch over one shared
document, which that task only reads (`libs_own_shared.go`).

The general rules for Go adapters are in [../gokit/README.md](../gokit/README.md).
