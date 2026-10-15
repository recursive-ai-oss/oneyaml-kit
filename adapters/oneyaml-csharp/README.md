# oneyaml-csharp adapter

The kit adapter for the `OneYaml` library (oneyaml-csharp). Unlike the other .NET adapters it does
not pin a package: the kit does not fetch the library, it builds it from a checkout on the machine.

## Pointing it at a checkout

```sh
export KIT_ONEYAML_CSHARP_SRC=/path/to/oneyaml-csharp   # any directory with src/OneYaml/OneYaml.csproj
bin/kit fast --adapter oneyaml-csharp
bin/bench run --mode quick --lang csharp                # the harness links the same checkout
```

The variable can name a clone, a snapshot or a worktree. `build.sh` links the library by a project
reference, so no tracked file changes, and passes the checkout's commit as the version in the report
(`devel` when it is not a git checkout). `KIT_DOTNET_ARTIFACTS` is passed as the artifacts path, which
moves the library's own `bin` and `obj` folders too: nothing is written into the checkout or this
folder. `DOTNET` names the dotnet executable when it is not on `PATH`.

## Without the variable

The adapter is **unavailable**, not failed (`requires_env` in `adapter.json`).
`bin/kit fast` prints
`oneyaml-csharp: SKIPPED, not available: environment variable KIT_ONEYAML_CSHARP_SRC is not set`,
the report lists it under "Not run", `bin/gate` stays green and the harness builds without the
library. The project still loads without the checkout (the reference and the `ONEYAML_LINKED`
define exist only when `build.sh` passes the checkout as `OneYamlSrc`), so formatting runs everywhere
and never touches the checkout.

## What it declares

Capabilities: `events`, `value`, `error-category`, `error-position`, `patch:add`, `patch:remove`,
`patch:replace`, `patch:move`, `patch:copy`, `patch:test`, `patch:merge`. The library is given the input bytes and detects the encoding itself, so nothing is
decoded for it.

| Operation | Through | Supplied by the adapter |
|---|---|---|
| `events` | the nodes of `YamlDocument.Parse` (kind, style, tag, anchor, alias) | `style`: whether a document had an explicit `---` or `...`, from a line scan of the decoded text. `tag`: the full tag from the written tag and the `%TAG` directives of that scan |
| `value` | the same nodes read with `GetValue<object>`, so key types, repeated keys and tags survive (the generic `object` model of the serializer drops tags and reads keys as text) | a tag the Core schema does not resolve is written as `$tagged` |
| `patch` | `YamlDocument.ApplyPatch` (RFC 6902: all six operations, any number, atomic) and `ApplyMergePatch` (RFC 7396), given the patch as the case carries it | nothing |

There is no `error-events`: the library's event reader is not public, and a document that fails to parse
gives no events. A library `DuplicateKey` error is reported as `duplicate-key`. Every case the library fails is
listed in `deviations.json` with the reason; the kit fails a run when a failure is missing from the list or an
entry has gone stale.

The benchmark side is `bench/csharp/src/Libs/OneYamlLibs.cs`, built in only with the same variable. It has
one row per binding path (`oneyaml-csharp`, reflection; `oneyaml-csharp-gen`, a generated context for the
kit's models) and takes part in every task.

The shared plumbing is in [../dotnet-common/README.md](../dotnet-common/README.md).
