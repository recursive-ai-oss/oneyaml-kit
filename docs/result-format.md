# Result format

`bin/kit fast` writes one JSON file per adapter, `results/<adapter>.json` in the
output directory (`$KIT_OUT`, default `./out`, never committed). Reports are
generated from these files only.

```json
{
 "format": "oneyaml-kit-results/2",
 "kit": {
  "version": "0.1.0",
  "corpus_digest": "<sha-256 of the pins>",
  "suites": {
   "yaml-test-suite": {"release": "data-2022-01-17", "revision": "<commit>", "scored": true},
   "own": {"cases": 39}
  }
 },
 "run": {
  "date": "2026-10-14", "mode": "fast", "complete": true, "cases": 720,
  "seconds": 0.2, "adapter_restarts": 0, "bounded_sessions": 0,
  "system": {"os": "...", "machine": "...", "cpus": 32, "load_1m": 1.1, "python": "3.13.13"}
 },
 "adapter": {
  "name": "oneyaml-rust", "version": "1.1.0", "language": "rust", "protocol": 1,
  "capabilities": ["events", "value"], "info": {}
 },
 "cases": [
  {"id": "yts:229Q", "suite": "yaml-test-suite", "category": "parse", "op": "events",
   "expect": "events", "loose": "pass", "strict": "pass", "micros": 41}
 ],
 "deviations": {"listed": 17, "unlisted": [], "stale": [], "unknown": [], "ok": true, "entries": []}
}
```

In `run`, `adapter_restarts` counts how often the shared adapter process was started
again after a failure (a restart that failed, or whose hello differed from the first one, is not counted: its case failed with `detail`), and `bounded_sessions` how many separate processes the bounded cases
used (one each, see the adapter protocol).

An adapter that cannot start at all has no result file; the command line and the generated report list it as an adapter that did not start (see the adapter protocol).

## Cases

Per case: `loose` and `strict` are each `pass`, `fail` or `unsupported`.

- `detail` explains the first difference for a failure.
- `reason` is `capability` (the adapter does not declare what the case needs, in
  which mode the verdict is unsupported) or `adapter` (the adapter declined the case).
- `corrected` is present when the case has a corrected expectation and holds the same
  two verdicts judged against it, with its own `detail`.
- `micros` is the wall-clock time of the round trip; it is information, not a score.
- `got` (for error and bounded expectations) is what the library actually returned: its
  status, error category, position and message, the number of events, so a verdict can be
  audited without re-running.
- `measured` (bounded cases): `seconds` (from the request completely written to the last byte of the
  answer read), `time_bound` (the time bound that was enforced:
  the case's own bound, a hang guard, see the case format) and `peak_mb`,
  the peak resident-memory growth (null where it cannot be measured). A case whose adapter
  could not be started has `seconds` 0 and `peak_mb` null.
- `tags`: the kit case's tags, for kit cases that have any (for example `convention`), so a
  report can count verdicts that rest on a kit rule apart from the rest.
- `emulated`: the behaviours the adapter supplied for this answer (see the adapter
  protocol). The adapter record lists what it may emulate in `emulates`.

A crash, a timeout or a malformed answer is a `fail` in both modes, with the reason in
`detail`.

## Deviations

`deviations` is the outcome of checking the adapter's list against the run (see the
adapter protocol). `ok` is false when anything is unlisted, stale or unknown, and the
command then exits with a non-zero status. `entries` repeats the list with its
reasons so the report can show it.

## Reports

`bin/kit report results/*.json --out conformance.md` renders Markdown: for each
library a table of groups with the case count, loose and strict passes, strict passes
against the corrected expectations, the unsupported count, the cases unsupported in
strict mode only, and the passes that depended on adapter emulation, then its list of known
deviations. The scored suite version, the kit version and the corpus digest are in the
header. A pass count is always out of every case in its group.

The report goes through the same public-text check as hand-written text, and
`bin/kit fast` fails if it does not pass.

## Fast mode and system record

`run.mode` is `fast` when no case tagged `slow` ran, and `full` when at least one did.

Fast mode runs in-process in seconds: no stress or exhaustive cases (tagged `slow`)
and no timing claims. Each run records the date, versions, operating system, CPU count
and load, because a result is only comparable with its conditions. Numbers from a
shared host are smoke results.
