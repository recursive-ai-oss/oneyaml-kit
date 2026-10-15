# .NET benchmark harness

The in-process harness for the .NET YAML libraries. It follows the contract in
[docs/benchmark-adapters.md](../../docs/benchmark-adapters.md): one program that checks every answer
with the guard before it times it, and writes one JSON line per (library, task, workload) cell.

```sh
bin/bench run --lang csharp                   # quick mode, writes out/bench/
bin/bench run --lang csharp --task dump --workload gha-workflow
```

The .NET SDK 10 is needed (`DOTNET` names the executable when it is not on `PATH`). Build output goes
under the driver's build directory, never into this folder.

## What it measures

| Id | Package | API used | Tasks |
|---|---|---|---|
| `yamldotnet` | YamlDotNet 18.1.0 | `DeserializerBuilder` / `SerializerBuilder`, reflection | typed-load, generic-load, dump |
| `sharpyaml` | SharpYaml 3.14.0 | `YamlSerializer`; typed models through a source-generated `YamlSerializerContext`; untyped as `object` | typed-load, generic-load (one document), dump |
| `sharpyaml-model` | SharpYaml 3.14.0 | `YamlStream.Load`, nodes written back with `ToString` | generic-load (every document), dump |
| `sharpyaml-syntax` | SharpYaml 3.14.0 | `YamlSyntaxTree.Parse`, `ToFullString`, `WithTextChange` | document-parse, unchanged-emit, edit-script |
| `vyaml` | VYaml 1.4.0 | `YamlSerializer` over UTF-8 bytes; typed models marked `[YamlObject]` so its source generator writes the formatters | typed-load, generic-load, dump |
| `yaml2jsonnode` | Yaml2JsonNode 3.0.1, JsonPatch.Net 5.0.3 | YAML to `JsonNode` and back; System.Text.Json for typed models; RFC 6902 on the tree | typed-load, generic-load, dump, patch |

Optional rows, built in only when the machine has the library:

| Id | Package | API used | Tasks |
|---|---|---|---|
| `oneyaml-csharp` | OneYaml, from the checkout named by `KIT_ONEYAML_CSHARP_SRC` | reflection binder (`YamlSerializer.Deserialize<T>` over UTF-8 bytes); `YamlDocument` for the document tasks | typed-load, generic-load, document-parse, unchanged-emit, edit-script |
| `oneyaml-csharp-gen` | the same | source-generated binder (a `YamlSerializerContext` the library's generator builds from `src/Models.cs`); the same `YamlDocument` code for the document tasks | the same five |

`KIT_ONEYAML_CSHARP_SRC` names a checkout of oneyaml-csharp (a directory with `src/OneYaml/OneYaml.csproj`),
linked as a project reference with its commit as the version. Without it the harness is exactly the
competitors above and `packages.lock.json` applies. With it the restore is not locked (a project reference
has no place in the lock file), the competitor packages are still exact versions, and the lock file that
restore writes goes to the build's obj folder, not here. The two ids are separate rows so each path is
compared with the strongest competitor on its own footing. dump and patch are `unsupported` until the library has
`Serialize` and `ApplyPatch`. The generator is built from the same checkout and added as an analyzer, as a package would
bring it; it is build-time only and adds nothing to the restore that `packages.lock.json` covers.

A task a library cannot do is `unsupported`, with the reason: there is no lossless document in
YamlDotNet, VYaml or Yaml2JsonNode, and no patch engine except JsonPatch.Net's. `yaml2jsonnode` is the
patch baseline of the ecosystem and is **not lossless**: it parses the document into a JSON tree, applies
the patch there and writes a new document, so comments, blank lines, quoting and layout are gone and its
patch cells are `differs`. They are listed as known deviations; they carry no number.

Each library is driven the way its documentation shows, with its defaults. The choices that matter are
recorded in each library's `settings` in the result file:

- YamlDotNet is used with reflection, as most of its users do. Reading untyped data uses
  `WithAttemptingUnquotedStringTypeDeserialization`, the documented way to get numbers and booleans and
  not strings out of an `object`.
- SharpYaml and VYaml are used with their source generators, which is what their documentation recommends
  and how their users get their speed. SharpYaml's untyped read needs `ReferenceHandling.Preserve` for any
  input with an alias, so it is set for reading and not for writing.
- Limits are the libraries' defaults. SharpYaml refuses nesting deeper than 64 and YamlDotNet's serializer
  stops at 50, so the two depth-100 workloads (`deep-block`, `deep-flow`) are `rejects` for them.
- The edit task for `sharpyaml-syntax` finds each edit's node in the tokens of the tree that was parsed
  before the clock, writes the new value's text (the library replaces a range of characters and has no path
  or value API) and applies the text changes, last edit first, all inside the clock. The lookup and the writing
  of the new text (style, quoting, block header, chomping) are the harness's own code, not the library's, so
  these cells carry the variant **`harness-rendered`**: they are listed as known deviations, shown apart from
  the other rows, marked †, and never called the best of a row (a row is still compared with them when they are ahead).
- Limits are never raised. SharpYaml's depth limit of 64 and YamlDotNet's serializer limit of 50 are defaults
  a user gets, so a workload that nests deeper is `rejects` with the reason "default limit, not raised".

The typed models are in `src/Models.cs`: the same fields, in the same shapes, as the other harnesses'
models. A field that holds either of two shapes (a job's `container` is a string or a mapping, a step's
`with` value is a string or a boolean) is an `object`, which is the plain way to take a union without
writing a converter for each library.

## The reference parsers

A `dump` is judged by reading its output back. The harness uses the event parsers of two libraries
(YamlDotNet's and SharpYaml's, with no depth limit) and one core-schema resolver of its own
(`Referee.Resolve`, YAML 1.2.2 section 10.3.2), so the text is read the way the specification reads it and
not by a library's idea of a type. When the two parsers read it differently, or only one accepts it, the
cell is `unjudged`.

## Time and memory

The runtime runs with its defaults: tiered compilation, quick JIT and dynamic PGO are on, as in a user's
program. A method then runs as unoptimised code, then as instrumented code, then as optimised code shaped by
the profile, and each step waits for a timer (100 ms without new unoptimised code) and a call count. So every
cell is warmed first: it runs until the number of methods the JIT has compiled has not changed for a whole
settle window (250 ms; two windows in full mode, at least 300 ms and at most 12 s), and only then is the batch
calibrated and sampled. Each cell records `warmup_ms`, `warmup_settled` and `warmup_compiled_methods`, and the
harness record says in `flags` that quick JIT, tiered compilation and PGO are on and how the warm-up ends. A
cell that did not settle within the limit says so and its number should be read with that in mind.
Quick mode uses one settle window and a smaller limit; it is a smoke result.

**Equivalent work.** Every library gets UTF-8 bytes (the form of a file) and returns UTF-8 bytes, so
libraries that work on strings decode the input and encode the output inside the timed work, and VYaml,
which works on bytes, does not. This is stated in each library's settings. A task that only reads its
starting state (dump, unchanged-emit, edit-script, patch) runs every task of a batch over one shared state
instead of building one per task: the guard runs such a task twice over one state and refuses a runner whose
second answer differs (the contract allows this option in every harness, on this condition). A batch is always as
large as the sample target needs, however slow the states are to build; a cell whose states are slow to build
takes fewer samples in quick mode, and in full mode never fewer than 30 (`min_samples`), and not fewer tasks per
sample, because a one-task batch measures the fixed cost of an invocation.

**Memory.** The memory pass runs after the timing, so it sees the optimised code a user's program runs (unoptimised
code allocates more). `allocated_bytes` is exact (the bytes the task's own thread requested). `peak_bytes` is the
starting state (live bytes after a full collection) plus every byte the task requested, taken in a no-GC
region sized from an earlier run, so no collection happens during the run and the figure is the same
whatever ran before it and however the cells are filtered or ordered. It counts garbage the collector has
not yet freed, as the Go figure does, and is an upper bound on the live heap. `retained_bytes` is what one run leaves
live (pools of reusable arrays, caches): the live bytes one full collection after the run, less the live bytes just
before it, with the starting state held, the same quantity every harness reports. It has a pass of its own, after the
others, because the collections it needs would trim the pools they use. The
number of allocation calls is not available. Within a process the figures repeat to the byte; between processes
the bytes a task requests move by a few percent in steps, because dynamic PGO changes how many objects the JIT
keeps on the stack (without the profile they repeat to the byte). The definition, and what it hides, is in the adapter contract's
memory section.

Timing is BenchmarkDotNet (see the adapter contract for how a cell is run).

`--mode guard` runs only the guard and writes verdicts without numbers; the recorder of the known deviations
uses it, and it is not a measurement.

## Self-tests

`SELFTEST=1 ./build.sh BUILD_DIR` builds the self-test assembly: the control libraries (a right one, a
wrong one, a refusing one and one that ends the process) and the `selftest` command. It is a different
assembly name, so it cannot be mistaken for a measurement build. `bench/tests/test_csharp.py` builds it and
runs it.
