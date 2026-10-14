# .NET adapters

Four adapters put the .NET YAML libraries through the kit. They share the plumbing in this
directory (`dotnet-common`: the JSON Lines loop, input decoding, kit-value and event writers) and
hold no YAML code of their own.

| Adapter | Library (exact version) | Events | Values | Patches |
|---|---|---|---|---|
| `yamldotnet` | YamlDotNet 18.1.0 | `Parser` over a `StringReader` | `Deserializer` into `object`, one call per document | none: no patch or edit API |
| `sharpyaml` | SharpYaml 3.14.0 | `Parser.CreateParser` | `YamlStream.Load`, then `ToObject<object>()` | `replace` of a scalar through `YamlSyntaxTree.WithTextChange` |
| `vyaml` | VYaml 1.4.0 | `YamlParser.FromBytes`, style from `Utf8YamlTokenizer` | `YamlSerializer.DeserializeMultipleDocuments<object>` | none |
| `yaml2jsonnode` | Yaml2JsonNode 3.0.1 with JsonPatch.Net 5.0.3 | accepted or rejected only (`YamlSerializer.Parse`) | `YamlSerializer.Parse(...).ToJsonNode()` | all six RFC 6902 operations on the JSON tree |

`yaml2jsonnode` pins YamlDotNet 18.1.0 itself (the package only asks for 16.3.0 or newer), so its parser
is the one the `yamldotnet` row measures. Every adapter pins its libraries with an exact version range in
its project file, and `packages.lock.json` fixes the rest; once a lock file exists the restore must match
it, so a version moves only on purpose (delete the lock file, change the version, check the diff).

A fifth, `oneyaml-csharp`, is optional: it links the oneyaml-csharp library from a local checkout
(see [its README](../oneyaml-csharp/README.md)) and declares nothing yet.

## Running

```sh
bin/kit fast --adapter yamldotnet --adapter sharpyaml --adapter vyaml --adapter yaml2jsonnode
```

Each adapter builds once (`build.sh`) and then runs as a prebuilt program (`run.sh` starts it with
`exec`, so the runtime stays in the process the kit started and its memory is measured directly).
The .NET SDK 10 is needed. `DOTNET` names the `dotnet` executable when it is not on `PATH`, and
`KIT_DOTNET_ARTIFACTS` says where build output goes (default: `out/artifacts` under the repository,
which is ignored; never an adapter folder). A folder named `artifacts`, `bin` or `obj` left inside a
project by some other tool is not source: the project defaults exclude it, so it cannot break the next
build. A cold build of all four takes a few seconds on a developer machine; the whole fast set then
runs in about two seconds per adapter.

## What an adapter reports

The adapters declare only what the library's own API can do.

- All four adapters speak protocol 2. A library with no event stream (`yaml2jsonnode`) declares
  `outcome:events`: it is asked the event cases and answers `accepted` or `error`, so an invalid input
  it takes is a failure, while its events are not claimed. A library with no patch API is never sent
  patch cases.
- A value the library returns as a shared alias graph has no finite description. The adapter answers
  `accepted`, and the kit grades the time and memory it measured.
- **Only the library's own exceptions are rejections.** Each adapter names the exception types the
  library raises to report a problem with the input (`YamlDotNet.Core.YamlException`,
  `SharpYaml.YamlException`, `VYaml.YamlException` and its subclasses; `yaml2jsonnode` also the
  pointer and JSON patch parse exceptions of JsonPatch.Net). Anything else escaping a library call, such
  as an invalid-operation error from a scanner, a failed cast or the runtime's own duplicate-key error,
  is a defect in the library, not a rejection of the input: the adapter ends the process, the kit
  records a crash for that case and starts a new adapter. A stack overflow ends it in the same way.
- **Error categories** come from the exception type the library chose, never from message text.
  A library that raises one type for every failure reports `other`, and a strict check on the
  category then fails: that is the library's gap, not the adapter's.
- **Positions** are those of the exception, converted to the kit's 1-based line and column when the
  library counts from zero. YamlDotNet and SharpYaml count columns in UTF-16 code units, so a character
  outside the Basic Multilingual Plane makes every later column on its line one too high; the adapters
  convert the column to characters (`Positions.cs`). VYaml's exceptions carry no readable position, so
  `vyaml` does not declare `error-position`.
- Values keep the library's own typing. A library that returns a single-precision float for a YAML
  float, or reads every number as a decimal, is judged on exactly that.
- A case the adapter cannot translate (for VYaml, an event whose style the tokenizer cannot attribute
  with certainty, or a stream with `%TAG` handles that the library does not expose) is answered
  `unsupported` with the reason, and is listed in the deviation list.
- VYaml declares `error-events`: the events it reports before an error are the ones its parser produced,
  with style and tag text supplied as declared (`style`, `tag`). Where its parser stops earlier or later
  than the specified events, strict mode fails.
- Library code runs on a thread with an 8 MiB stack, the Linux default for a program's main thread.
  A library that recurses on nesting depth overflows it at the depth it would overflow in an ordinary
  program; the process dies, the kit records a crash for that case and starts a fresh adapter.
  The adapter's own conversions run on a much larger stack so that they are never what overflows.
- The kit's `memory_limit_mb` cap is not used: the runtime reserves address space far beyond what it
  touches. The kit's resident-memory bounds on safety cases apply as usual.

## Behaviour the adapters supply

Whatever an adapter does that the library does not is declared in its hello (`emulates`) and named in
every answer that depended on it, so reports count it separately.

| Name | Used by | Meaning |
|---|---|---|
| `decode` | all | The library reads a string, so the adapter detects the encoding (YAML 1.2.2 section 5.2), removes a byte order mark, converts UTF-16 and UTF-32 and rejects malformed UTF-8. Plain valid UTF-8 is passed on without being named. VYaml reads UTF-8 bytes directly, so only UTF-16 and UTF-32 are converted for it. |
| `style` | `vyaml` | Scalar style, flow or block form and explicit document markers, read from the public tokenizer and matched to the parser's events in order (`TokenFacts.cs`). When the match is not certain the adapter declines the case. |
| `tag` | `vyaml` | The full tag written out from the handle and suffix the parser gives, for the `!`, `!!` and verbatim forms. A `%TAG` directive makes the adapter decline. |
| `pointer` | `sharpyaml` | The JSON Pointer resolved to the source span of a scalar, from the library's own parse events: an alias is followed to the node it names, and of two equal keys the first in document order is taken. The adapter refuses neither a write through an alias nor an ambiguous key. |
| `source-text` | `sharpyaml` | The YAML text for the new value (style kept where it can hold the value, block scalar header and indentation, quoting), because `WithTextChange` only swaps raw text. |
| `error` | `sharpyaml`, `yaml2jsonnode` | An error the adapter raised itself: a missing path, a malformed pointer, an unknown operation, a missing document. |
| `unchanged` | `sharpyaml` | The original bytes reported after any failed step; the library applies one text change at a time. |
| `classification` | `yaml2jsonnode` | JsonPatch.Net reports a failed operation as a message and an operation number; the adapter calls a failed `test` `patch-test` and any other failure `patch-path`. |

`sharpyaml` only replaces a scalar with a scalar. Replacing a collection, adding, removing, moving,
copying, testing and merge patches are not declared, because each would need the adapter to write
structure the library has no API for.

`yaml2jsonnode` has no text to preserve: the document it writes after a patch is a fresh serialisation
of the data, in UTF-8 without a byte order mark, with every document of the stream written again.
JsonPatch.Net works on a copy, so a failed patch leaves the caller's tree alone; the bytes reported for
"unchanged" are the serialisation of that untouched tree.

## Deviation lists

Each `deviations.json` lists every case the library does not pass (or the adapter declines), in loose
and strict mode and against both the upstream and the corrected expectation. The kit fails a run when
a failure is missing from the list or an entry no longer holds. The reasons are generated from the
result of the run, so they state what happened and not why; the cause of a class of failures belongs
in the library's own notes.
