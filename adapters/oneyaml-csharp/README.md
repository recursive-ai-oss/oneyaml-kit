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

No capability yet, so every case is unsupported and the deviation list is empty. `Program.cs` lists
the three operations (events, value, patch) as seams: what each will call, and which capabilities to
declare with it.

The benchmark side is `bench/csharp/src/Libs/OneYamlLibs.cs`, built in only with the same variable;
it takes part in no task yet and has one row per binding path (`oneyaml-csharp`,
`oneyaml-csharp-gen`).

The shared plumbing is in [../dotnet-common/README.md](../dotnet-common/README.md).
