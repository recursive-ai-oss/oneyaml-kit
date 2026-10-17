# Principles

The rules every case, adapter and report in the kit follows. The [case format](case-format.md),
[adapter protocol](adapter-protocol.md) and [result format](result-format.md) put them into
detail.

- **Cases are data.** A case states the expected behaviour as data: events, values, exact output
  bytes, error categories and positions. Inputs and expected bytes are stored raw; nothing
  normalises them.
- **Expectations come from the specification**, YAML 1.2.2 and the RFCs, never from a library's
  output. Where the upstream suite is wrong, our expectation, the reason and the citation are
  recorded beside it and both are scored.
- **Comparison is full.** Every byte, the whole event stream, the whole value. A narrower view
  is a named mode.
- **Results are per capability, strict and loose.** A case an adapter cannot do is counted as
  unsupported, separately from passes and failures.
- **Safety cases name the acceptable outcomes** (an exact result, a limit error, or both) and
  carry their own time and memory bounds, which the runner enforces and records.
- **Behaviour an adapter supplies itself is declared and marked.** Passes that depend on it are
  counted in the Loose and Strict columns and again in their own Emulated column, so a reader can
  subtract them.
- **Known deviations are listed per library and verified.** A new failure, or an entry that no
  longer holds, fails the run.
- **Reports are generated** from result files.
