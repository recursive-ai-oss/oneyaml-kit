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
- The edit task for `sharpyaml-syntax` works out each edit's source span and replacement text from the
  workload's pointers before the clock (the library replaces a range of characters and has no path or
  value API), so the clock sees only the text changes, last edit first. The span comes from the library's
  own event parser; the replacement keeps the style of the value it replaces.

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

The project file turns quick JIT and dynamic PGO off (and the harness records both), so library code starts
as fully optimised machine code. With them on, a method that a short run calls only a few times per sample
stays in unoptimised tier-0 code, and what a cell measures depends on how many times the run happened to
call it: the same cell was six times slower when its batch held one task than when it held four. Programs
that run long enough to tier up and profile will run faster than these figures by the amount PGO buys.

Timing is BenchmarkDotNet (see the adapter contract for how a cell is run). Memory is taken in a separate
run: allocated bytes are exact, the peak is the starting state plus the growth of the managed heap, and
the number of allocation calls is not available. The definition, and what it hides (memory a library keeps
for reuse, such as array pools), is in the adapter contract's memory section.

## Self-tests

`SELFTEST=1 ./build.sh BUILD_DIR` builds the self-test assembly: the control libraries (a right one, a
wrong one, a refusing one and one that ends the process) and the `selftest` command. It is a different
assembly name, so it cannot be mistaken for a measurement build. `bench/tests/test_csharp.py` builds it and
runs it.
