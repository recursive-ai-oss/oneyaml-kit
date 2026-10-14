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

The library under test is pinned to an exact version in the adapter's own manifest
and lockfile. The adapter reports that version in its hello.

## Messages

Kit to adapter:

```json
{"type": "hello", "protocol": 1}
{"type": "run", "id": "own:edit/sequence-item", "op": "patch", "input": "<base64>", "args": {...}}
{"type": "bye"}
```

`input` is the exact input bytes, base64. `args` is the operation object of the case
without `type`: for `patch`, `patch`, `format`, `document`; and `profile` when set.

Adapter to kit, first line:

```json
{"type": "hello", "protocol": 1, "name": "oneyaml-rust", "version": "1.1.0",
 "language": "rust", "capabilities": ["events", "value", "patch:replace", "error-category"],
 "emulates": ["unchanged", "error"],
 "info": {"anything": "free-form, recorded in the results"}}
```

then one response per `run`, with the same `id`:

| `status` | Fields |
|---|---|
| `ok` | `events` (array of lines) for `events`; `value` (list of documents) for `value`; `output` (base64 bytes of the whole stream after the patch) for `patch` |
| `error` | `error`: `{"category", "message", "line", "column"}` (`line` and `column` optional; the conventions are in the case format's *Positions*). For `events`, also `events`: the lines produced before the error. For `patch`, also `output`: the bytes of the document as the library would now write it, so that "unchanged" can be checked |
| `unsupported` | `reason`: a short sentence. The adapter declines this case |

A response may also carry `emulated`: a list of the behaviours (declared in the hello's
`emulates`) that this answer depended on. See *Behaviour the adapter supplies*.

An error `category` is one of the kit's names (`syntax`, `encoding`, `limit`,
`patch-path`, `patch-test`, `patch-invalid`, `refused-alias`, `refused-ambiguous`)
or `other` for anything the adapter cannot map. `message` is for people and is never
compared.

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
| `profile:<name>` | a behaviour profile other than `core` |

A case needing something not declared is not sent. It is reported as unsupported,
which is not a pass and is counted separately from failures. A strict check that needs `error-category` or
`error-position` is reported as unsupported in the same way. Declaring a capability
is a claim: every case that needs it is then judged.

An adapter may also answer `unsupported` for one case it cannot translate (for
example a patch shape its library has no equivalent for). That answer must be
listed as a known deviation, so declining cannot go unnoticed.

## Behaviour the adapter supplies

A library without a patch engine may be driven through an adapter that translates the
operations that map directly onto its edit API and declares only those. Some behaviour
cannot be translated and has to be supplied by the adapter: discarding a half-applied
patch, or raising an error the library does not raise (a missing path, a malformed
pointer). The kit must never credit that to the library, so it is declared and marked:

- the hello lists every kind of behaviour the adapter may supply in `emulates` (the
  rust adapter uses `unchanged`, for reporting the original bytes after a failure, and
  `error`, for an error the adapter raised itself);
- every response that depended on one names it in `emulated`. A response naming
  something the hello did not declare fails that case;
- results keep the names per case, and reports count the passes that depended on them
  in their own *Emulated* column, in addition to Loose and Strict.

An adapter reports the library's own state wherever it can. Emulating is for what the
library has no way to express, and the more an adapter emulates, the less its row says
about the library.

## Limits and measurement

For a `bounded` case the runner uses the case's own time bound as the timeout, polls the
adapter's resident memory while the case runs and kills the process if its growth passes
the case's memory bound, and afterwards reads the peak resident memory since the case
started. **The whole process tree is measured**, not only the process the kit starts: the adapter
is started in its own session and every live process of that session counts, so a
launcher that does not exec the library (a shell script, `go run`, `dotnet run`) cannot
hide its memory. A hard requirement for adapters: whatever launches the library must stay
in that session (do not detach with `setsid`, a double fork into another session, or a
container that hides the processes). On Linux this uses `/proc`; where the tree cannot be
measured the peak is reported as unmeasured and strict reports unsupported. Everything
in the session is killed when a case fails or the run ends. Adapters do nothing special. A killed or timed-out adapter fails that case
and a fresh process serves the next one.

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
