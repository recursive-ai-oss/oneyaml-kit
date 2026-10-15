# Adapter protocol

An adapter connects one library, in any language, to the kit. It is a small program
(or a wrapper around one) that speaks JSON Lines on standard input and output: one
JSON object per line, UTF-8, no pretty printing. Anything on standard error is
ignored. One process serves a whole run, so start-up cost is paid once.

The kit side sends requests and judges answers. The adapter never sees an
expectation and never compares anything, so every library is judged by the same code.

## Manifest

`adapters/<name>/adapter.json`:

```json
{
  "format": "oneyaml-kit-adapter/1",
  "name": "oneyaml-rust",
  "language": "rust",
  "build": ["cargo", "build", "--release", "--locked", "--quiet"],
  "command": ["./run.sh"],
  "timeout_seconds": 10,
  "memory_limit_mb": 8192,
  "deviations": "deviations.json"
}
```

- `build` (optional) runs once in the adapter directory before the run.
- `command` starts the adapter, in the adapter directory.
- `timeout_seconds` is the time allowed for one ordinary case (a `bounded` case states its
  own time and memory bounds, which always apply instead); an adapter that does not answer
  in time is killed, the case fails, and a fresh process serves the next case.
- `memory_limit_mb` (optional, POSIX) caps the adapter's address space.
- `deviations` names the list of known deviations (below).
- `requires_env` (optional) lists environment variables that must be set and non-empty for the
  adapter to run, for a library the kit cannot fetch itself. When one is missing the adapter is
  *unavailable*: `bin/kit fast` prints `SKIPPED, not available: <why>`, the report lists it
  under "Not run", and the run is not a failure. It is never built or started.

The library under test is pinned to an exact version in the adapter's own manifest
and lockfile. The adapter reports that version in its hello.

## Messages

Kit to adapter:

```json
{"type": "hello", "protocol": 2}
{"type": "run", "id": "own:edit/sequence-item", "op": "patch", "input": "<base64>", "args": {...}}
{"type": "bye"}
```

`input` is the exact input bytes, base64. A case that generates its input (docs/case-format.md) is sent the same way, so an input of tens of megabytes is one long line; read the line whole. `args` is the operation object of the case
without `type`: for `patch`, `patch`, `format`, `document`; and `profile` when set.

Adapter to kit, first line. `protocol` is the version the adapter speaks: 2, or 1 for an
adapter that has not moved yet (see *Protocol versions*):

```json
{"type": "hello", "protocol": 2, "name": "oneyaml-rust", "version": "1.1.0",
 "language": "rust", "capabilities": ["events", "value", "patch:replace", "error-category"],
 "emulates": ["unchanged", "error"],
 "info": {"anything": "free-form, recorded in the results"}}
```

then one response per `run`, with the same `id`:

| `status` | Fields |
|---|---|
| `ok` | `events` (array of lines) for `events`; `value` (list of documents) for `value`; `output` (base64 bytes of the whole stream after the patch) for `patch` |
| `error` | `error`: `{"category", "message", "line", "column"}` (`line` and `column` optional; the conventions are in the case format's *Positions*). For `events`, also `events`: the lines produced before the error. For `patch`, also `output`: the bytes of the document as the library would now write it, so that "unchanged" can be checked |
| `accepted` | (protocol 2) The library took the input and the adapter cannot or need not describe the result. No other field is needed. Only for `events` and `value` cases. See *Accepted without a result* |
| `unsupported` | `reason`: a short sentence. The adapter declines this case |

A response may also carry `emulated`: a list of the behaviours (declared in the hello's
`emulates`) that this answer depended on. See *Behaviour the adapter supplies*.

An error `category` is one of the kit's names (`syntax`, `encoding`, `limit`,
`patch-path`, `patch-test`, `patch-invalid`, `refused-alias`, `refused-ambiguous`, `unresolved-tag`, `duplicate-key`)
or `other` for anything the adapter cannot map. `message` is for people and is never
compared.

An `error` answer to an `events` case may leave `events` out when the library cannot say
what it had read before the error; the adapter then does not declare `error-events`
(below).

An adapter must answer every request, never exit on its own, and treat any input as
hostile: a crash, a hang or an unparseable line fails that case only.

## Capabilities

The hello lists what the adapter can do:

| Capability | Meaning |
|---|---|
| `events` | event streams |
| `value` | values |
| `patch:add`, `patch:remove`, `patch:replace`, `patch:move`, `patch:copy`, `patch:test` | RFC 6902 operations |
| `patch:merge` | RFC 7396 merge patches |
| `error-category` | errors carry a category that follows the kit's names |
| `error-position` | errors carry `line` and `column` that follow the kit's definition |
| `error-events` | (protocol 2) an `error` answer to an `events` case carries the events produced before the error, as the specification's notation has them |
| `outcome:events`, `outcome:value` | (protocol 2) the adapter can say whether an input is accepted or rejected for that operation, without describing the result. It is enough to be asked the cases that need only that |
| `profile:<name>` | a behaviour profile other than `core` |

An adapter declares `events` only when it can describe the event stream of an accepted
input, and `value` only when it can describe values. A library without an event stream
declares `outcome:events` instead: it is then asked the `events` cases and answers
`accepted` or `error`, so an invalid input it accepts is a failure and a valid input it
rejects is a failure, while the events themselves are not claimed.

A case needing something not declared is not sent. It is reported as unsupported,
which is not a pass and is counted separately from failures. A strict check that needs `error-category` or
`error-position` is reported as unsupported in the same way. Declaring a capability
is a claim: every case that needs it is then judged.

An adapter may also answer `unsupported` for one case it cannot translate (for
example a patch shape its library has no equivalent for). That answer must be
listed as a known deviation, so declining cannot go unnoticed.

## Accepted without a result

Some libraries cannot describe what they read: they have no event stream, or the result is
a shared graph the adapter cannot turn into a value without expanding it. For those the
adapter answers `{"id": ..., "status": "accepted"}`. The kit grades it like this:

| The case expects | An `accepted` answer is |
|---|---|
| an error (`error`) | a failure in both modes: the input was accepted |
| a result (`events`, `value`) | unsupported: the result cannot be compared. It needs no deviation entry when the adapter does not declare the full capability (`events`, `value`); if it does declare it and still answers `accepted`, the adapter declined the case and it must be listed |
| a `bounded` outcome | graded on the cost the kit measured. Over the time or memory bound: failure. Within the bounds, when the case allows only an error: failure. Within the bounds, when a result was also allowed: unsupported (the result was not described) |
| a patch result, or a `bounded` patch case | a protocol violation and a failure |

The kit measures time and memory itself for `bounded` cases, so an `accepted` answer is
enough for the cost to be recorded in the results.

## Protocol versions

The runner speaks version 2 and accepts adapters that say hello with 1 or 2.

- **Version 1 adapters** keep working unchanged. One that declares `events` is credited
  with `error-events` (it has always sent the events before an error), may not declare
  `outcome:*`, and may not answer `accepted`.
- **Moving to version 2** changes only the hello: `"protocol": 2`, plus the capabilities
  below as they apply. Nothing else about the messages changes.
  - A library with no event stream: remove `events`, declare `outcome:events`, and answer
    `accepted` instead of `unsupported` for any `events` case whose input the library
    took; remove the deviation entries that said "no event stream".
  - A library that reports errors without the events read before them: do not declare
    `error-events` and leave `events` out of `error` answers.
  - A library whose result for some accepted input cannot be described (a shared alias
    graph): answer `accepted` for that case instead of `unsupported`.
  - Every adapter: keep `emulates` within the vocabulary below.

## Behaviour the adapter supplies

A library without a patch engine may be driven through an adapter that translates the
operations that map directly onto its edit API and declares only those. Some behaviour
cannot be translated and has to be supplied by the adapter: discarding a half-applied
patch, or raising an error the library does not raise (a missing path, a malformed
pointer). The kit must never credit that to the library, so it is declared and marked:

- the hello lists every kind of behaviour the adapter may supply in `emulates`, using
  only the names of the vocabulary below. The runner refuses a hello with any other name;
- every response that depended on one names it in `emulated`. A response naming
  something the hello did not declare fails that case;
- results keep the names per case, and reports count the passes that depended on them
  in their own *Emulated* column, in addition to Loose and Strict.

### Vocabulary

| Name | Meaning |
|---|---|
| `unchanged` | The original bytes are reported after a failed patch, because the library applies one edit at a time or returns nothing when it fails. |
| `error` | An error was raised by the adapter, not the library: a missing path, a malformed pointer, an unknown operation, a missing document. Never a refusal the library does not make (see *Resolving pointers*). |
| `pointer` | A JSON Pointer was resolved to a node (or its source span) by the adapter, because the library has no addressing of its own. |
| `decode` | Text encoding was handled by the adapter (detecting UTF-8, UTF-16 or UTF-32, removing a byte order mark, rejecting malformed input) because the library reads strings. |
| `style` | Scalar style, flow or block form or explicit document markers were recovered by the adapter from another view of the input (for example a tokenizer), because the library's own events do not carry them. |
| `tag` | A full tag was written out by the adapter from the handle and suffix the library gives. |
| `source-text` | The YAML text of a new value (style, block header, indentation, quoting) was written by the adapter, because the library only swaps raw text. |
| `classification` | The adapter mapped a failure the library reports only as a message to a kit error category. |

Adding a name is a change to the kit (`tools/oneyaml_kit/vocabulary.py` and this table), so
the same word means the same thing for every library.

### Resolving pointers

An adapter for a library that has no addressing of its own resolves a JSON Pointer by what a
user of that library's API would naturally do, and marks `pointer`. It supplies **neither**
of these protections, so that the cases about them show what the library does:

- **Aliases.** It follows an alias to the node the library exposes as its target, and the
  edit lands there. It does not refuse a write through an alias, and it does not copy the
  value in place.
- **Duplicate or ambiguous keys.** It matches a key with the library's own lookup when the
  library has one, and otherwise takes the first match in document order. It does not look
  for a second match and does not refuse.

The adapter raises its own error (marked `error`) only for what the pointer itself says: a
malformed pointer, a path that does not exist, an index that is out of range, an
operation name that is not RFC 6902. A library that does refuse (it rejects duplicate keys
when it loads, say) is credited with that refusal, because it is the library's.

An adapter reports the library's own state wherever it can. Emulating is for what the
library has no way to express, and the more an adapter emulates, the less its row says
about the library.

## Limits and measurement

For a `bounded` case the runner starts a new adapter process (the same command, a full
hello handshake) that serves that case alone, and ends it afterwards, so the case is
measured from a clean start and what it costs never depends on the cases before it. The
shared process that serves the ordinary cases is not touched. An adapter therefore starts
once for the run and once more for each bounded case it declares support for; a case it
does not declare is not sent and starts nothing. The runner uses the case's own time bound
(stretched for a slow machine, see the case format) as the timeout, polls the
adapter's resident memory while the case runs and kills the process if its growth passes
the case's memory bound, and afterwards reads the peak resident memory since the case
started (growth over what the new process held after its handshake). **The whole process tree is measured**, not only the process the kit starts: the adapter
is started in its own session and every live process of that session counts, so a
launcher that does not exec the library (a shell script, `go run`, `dotnet run`) cannot
hide its memory. A hard requirement for adapters: whatever launches the library must stay
in that session (do not detach with `setsid`, a double fork into another session, or a
container that hides the processes). On Linux this uses `/proc`; where the tree cannot be
measured the peak is reported as unmeasured and strict reports unsupported. Everything
in the session is killed when a case fails or the run ends. Adapters do nothing special. A killed or timed-out adapter fails that case
and a fresh process serves the next one. A bounded case whose process cannot be started or
does not complete the handshake fails that case, and nothing else.

A JSON object with a repeated key in an answer is refused (that case fails), because no
single meaning can be chosen for it; the same holds for case files.

Responses are read without recursion, so an answer nested a hundred thousand levels deep
is graded like any other (and an unterminated or malformed one fails only its own case).
Build output goes under the output directory by default (`target`, `gobin` and `artifacts`
inside it) unless `CARGO_TARGET_DIR`, `KIT_GO_BIN` or `KIT_DOTNET_ARTIFACTS` say
otherwise.

## Known deviations

`adapters/<name>/deviations.json`:

```json
{"format": "oneyaml-kit-deviations/1",
 "entries": [{"case": "own:edit/refuse-write-through-alias", "expectation": "primary",
              "mode": "loose", "verdict": "fail", "reason": "..."}]}
```

`expectation` is `primary` or `corrected`, `mode` is `loose` or `strict`, `verdict`
is `fail` or `unsupported`. The run fails when:

- a case fails (or is declined by the adapter) and is not listed;
- an entry is stale: the case now passes, the verdict differs, or the entry names a
  verdict the case cannot have;
- an entry names a case that is not in a complete run.

A listed failure still counts as a failure in every score. Unsupported because of an
undeclared capability is not listed.

## Writing an adapter

1. Pick the language's own build and process tools; any language works if it reads
   and writes lines.
2. Send the hello with honest capabilities and the library's exact version.
3. For each `run`, decode the input bytes, call the library, convert the result to
   the shapes above, and answer. Convert events to the event notation with the
   library's public API only.
4. Run `bin/kit fast --adapter <name>`, check every failure by hand to see whether it
   is the library or the adapter, then list the genuine ones in `deviations.json`
   with a one-sentence reason.
