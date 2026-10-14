# Case format

A case is language-neutral data: input bytes, one operation, one expectation. The
kit's own cases live in `suites/own/cases/<category>/<name>/`. The vendored
yaml-test-suite is read through the same model by a loader, so both run through one
pipeline.

Input and expected output are stored as raw files and read as bytes. Nothing decodes,
re-wraps or normalises them, so byte order marks, CRLF, trailing whitespace and
UTF-16/32 survive. Do not open these files in an editor that rewrites line endings,
and do not let a formatter touch `suites/`.

## Directory

```
suites/own/cases/edit/block-literal-strip-when-no-trailing-newline/
  case.json       metadata, operation, expectation (JSON, UTF-8)
  input.yaml      the input bytes, exactly (the name can be changed with "input")
  expected.yaml   expected output bytes, when the expectation points at a file
```

The case id is the path below `cases/` (`edit/block-literal-...`). The first path
component is the category. Results use the global id `own:<id>`. yaml-test-suite
cases are `yts:<ID>` (`yts:DK95/06` for numbered sub-cases); where the suite ships an
`in.json` for a valid case, a second case `yts:<ID>@value` compares the value.

## `case.json`

```json
{
 "format": "oneyaml-kit-case/2",
 "id": "edit/trailing-comment-kept",
 "title": "A comment after the value stays",
 "category": "edit",
 "origin": "spec",
 "productions": ["187"],
 "refs": ["RFC 6902 section 4.3"],
 "tags": [],
 "operation": {"type": "patch", "patch": [{"op": "replace", "path": "/port", "value": 9090}]},
 "expect": {"kind": "bytes", "file": "expected.yaml"}
}
```

| Key | Meaning |
|---|---|
| `format` | Always `oneyaml-kit-case/2`. |
| `id` | Must equal the directory path below `cases/`. |
| `title` | One line describing the behaviour, not the input. |
| `category` | `parse`, `schema`, `edit`, `diagnostics`, `safety`, `compat` or `roundtrip`. |
| `origin` | `spec`, `bug`, `fuzz`, `real-world`, `competitor` or `upstream`. |
| `productions` | YAML 1.2.2 production numbers the case exercises, as strings. Optional; used by a future spec-coverage report. |
| `refs` | Citations: spec sections, RFC sections, or a page of these docs. A `spec` case must have at least one. |
| `tags` | Free labels. `slow` keeps a case out of the fast mode. |
| `notes` | Optional reasoning for a reader. |
| `provenance` | Where the case comes from: optional `source` (URL), `revision`, `licence` and `reference` (a report, issue or paper), all non-empty strings. A `bug`, `fuzz` or `competitor` case must have `reference`; a `real-world` case must have `source` and `licence`. A case written for the kit says so in `reference`. |
| `input` | Input file name; default `input.yaml`. |
| `operation` | What to do with the input, see below. |
| `expect` | What must come back, see below. |

Unknown keys are rejected, so a typo cannot silently disable a check.

Expectations come from the specification (YAML 1.2.2, the RFCs, the edit rules in
[edit-semantics.md](edit-semantics.md)), never from what a library happens to print.

## Operations

| `type` | Input | The adapter returns |
|---|---|---|
| `events` | a YAML stream | the event stream in yaml-test-suite notation, or an error |
| `value` | a YAML stream | the list of documents as kit values (below), or an error |
| `patch` | a YAML stream | the stream's bytes after applying the patch, or an error |

`patch` takes `patch` (an RFC 6902 array, or an RFC 7396 document when
`format` is `merge-patch`), `format` (`json-patch`, the default, or `merge-patch`)
and `document` (index of the document in the stream, default 0). Pointers are RFC
6901. A patch is atomic: when any operation fails the document is unchanged.

Any operation may set `profile` to name a behaviour profile; the default is `core`
(YAML 1.2 core schema, spec-correct defaults). Compat cases name the profile they
expect (for example a YAML 1.1 reading); an adapter that does not declare
`profile:<name>` gets "unsupported" for them.

### Event notation

One line per event, as in yaml-test-suite's `test.event`: `+STR`, `-STR`, `+DOC`,
`+DOC ---`, `-DOC`, `-DOC ...`, `+MAP`, `+MAP {}`, `-MAP`, `+SEQ`, `+SEQ []`, `-SEQ`,
`=VAL :plain`, `=VAL 'single`, `=VAL "double`, `=VAL |literal`, `=VAL >folded`,
`=ALI *name`, with ` &anchor` and ` <resolved-tag>` after the kind. In `=VAL` text,
`\`, NUL, backspace, newline, carriage return and tab are written `\\`, `\0`, `\b`,
`\n`, `\r`, `\t`. Directives, comments and other trivia have no line. In a file the
lines end with `\n`; in the protocol they are a JSON array of strings.

### Kit values

A value is JSON with these rules, so that a YAML reading can be compared exactly:

- `null`, booleans and strings are themselves.
- Integers are JSON numbers without a decimal point or exponent, and may exceed 64
  bits. Floats are JSON numbers that have a decimal point or an exponent (`12.0`,
  `1e3`). `1` and `1.0` are different values, and comparison never relaxes that.
  Non-finite floats cannot be written as JSON numbers.
- Keys starting with `$` are reserved. There are exactly three escape forms, each a
  one-key object:
  - `{"$float": "inf"}`, `{"$float": "-inf"}`, `{"$float": "nan"}` for `.inf`, `-.inf`
    and `.nan`;
  - `{"$map": [[key, value], ...]}` for any mapping that is not a plain object;
  - `{"$tagged": {"tag": "!color", "value": ...}}` for a node carrying a tag the schema
    does not resolve. The tag is written as the specification resolves it (a local tag
    `!color`, a global tag `tag:example.com,2000:x`). Tags the schema resolves
    (`!!str`, `!!int`, ...) never appear.
- A mapping whose keys are all strings, none starting with `$`, is a JSON object. Every
  other mapping, including one with a key such as `$float`, is written in the `$map`
  form. An object that has a `$` key and is not exactly one escape form is rejected when
  a case loads and never equals anything when an adapter sends it, so data can never be
  mistaken for an escape. The object form and the `$map` form of the same mapping are
  equal. Mappings are unordered; sequences are ordered.

An upstream value comes from yaml-test-suite's `in.json`, which has no tags and was
produced by a converter that loses the difference between integers and floats
(`450.00` is `450`). Those cases compare with `tags: ignore`, a named projection that
unwraps `$tagged` on both sides and nothing else. Number typing stays exact, so a
library that reads `450.00` as an integer fails; where upstream is wrong about a
number, the correction is recorded (see below). Own cases cannot use `tags: ignore`.

### Positions

A position is a 1-based `line` and `column`.

- The column counts characters (Unicode scalar values) of the *decoded* text, not
  bytes. A byte order mark is not a character of the text and is not counted; text in
  UTF-16 or UTF-32 is counted after decoding.
- A line break is a line feed, a carriage return followed by a line feed (one break), or
  a carriage return alone (YAML 1.2.2 section 5.4). The first character after a break
  is column 1 of the next line.
- The position of an error is that of the first character that cannot belong to any
  valid document: the shortest prefix of the input that no continuation can make
  valid ends at that character. `a: b` then ` c:d` is valid (the scalar `b c:d`), so
  in `a: b` / ` c: d` the colon is acceptable and the space after it, at 2:4, is not.
- When every character is acceptable and the input simply ends too soon (an unclosed
  flow collection or quote), no character is at fault and the position is the end of
  the input: the point just after the last character. After a final line break that
  is column 1 of the following line.

## Expectations

| `kind` | Fields | Passes when |
|---|---|---|
| `events` | `file` (event lines) | no error, and the event lines are equal |
| `value` | `value` (list of documents) or `file` (JSON) | no error, and the documents are equal |
| `bytes` | `file` | no error, and every output byte is equal |
| `error` | `category`, `line`, `column`, `events_file`, `unchanged` | an error is reported (see below) |
| `bounded` | `time_seconds`, `memory_mb`, `accept`, `error` | see below |

For `error`, **loose** mode only requires that an error is reported, of any kind,
anywhere. **Strict** mode also checks whatever the case states:

- `category`: one of `syntax`, `encoding`, `limit`, `patch-path`, `patch-test`,
  `patch-invalid`, `refused-alias`, `refused-ambiguous`, or `any` (the default).
  Categories are stable names, never message text.
- `line` and `column`: see *Positions* above. `column` may be left out when only the
  line is fixed.
- `events_file`: the events that must have been produced before the error
  (`events` operations only).
- `unchanged`: after a failed patch the document is byte-for-byte the input
  (`patch` operations only).

A case never states more than it can justify: a check that is not stated is not made.

### Bounded outcomes (safety)

A `bounded` case names the acceptable outcomes and the resources it may use:

```json
"expect": {"kind": "bounded", "time_seconds": 10, "memory_mb": 1024, "error": "limit",
           "accept": {"kind": "events", "file": "expected.events"}}
```

- `accept` (optional) is an exact result of the operation's own kind. If the library
  answers, the whole result must equal it. A wrong or truncated result is a failure.
- `error` (optional, a category other than `any`) is a refusal that is acceptable.
- At least one of the two is required. A case with only `error` allows no result; a
  case with only `accept` allows no refusal.
- `time_seconds` and `memory_mb` are part of the case. The runner enforces them
  whatever the adapter manifest says: the case is cut off at the time bound, and the
  adapter process is killed if its resident memory grows past the memory bound while the
  case runs. Memory is the growth in peak resident memory over what the process held
  when the case started.
- Loose passes when the outcome is the accepted result or any error, inside both bounds.
  Strict also needs the named category. If memory cannot be measured on the platform
  (see the adapter protocol: the whole process tree is measured), the memory bound is not
  checked in loose mode, and strict is unsupported rather than a silent pass. The time
  bound is always checked.
- Results record what came back (`got`) and the measured time and memory.

## Upstream cases and corrections

`suites/yaml-test-suite/data-<release>/` is the vendored release, untouched. Where an
upstream expectation is wrong, `suites/yaml-test-suite/corrections.json` records ours
beside it:

```json
{"case": "yts:T833", "events": ["+STR", "+DOC ---", "+MAP {}", "=VAL :foo"],
 "reason": "...", "citation": "YAML 1.2.2 section 7.4.2 ..."}
```

A correction carries either `events` (the events before an error) or `value` (the
documents of a `@value` case). Both expectations are scored: results carry the verdict
against the upstream expectation and, for corrected cases, against ours. A correction
that equals the upstream value, or names an unknown case, is rejected.

### When a document ends before an error

The events before an error are not defined by the specification, so the kit only
corrects them by a written rule, applied to every recorded error tree: *a document ends
only at its end marker, at the start of the next document, or at the end of the stream;
an error found before any of these happens while the document is still open, so no
document end is emitted before it* (YAML 1.2.2 section 9.1, productions 211 and 205).
The recorded trees that disagree with the rule are corrected (4H7K, BS4K, EB22); the
ones that agree (9JBA, KS4U, 3HFZ, QLJ7) are left as recorded. Other timing differences
between libraries (how far ahead a parser reads) are not corrected and not claimed.

## Capabilities a case needs

The runner works out what an adapter must support from the case: `events`, `value`,
`patch:<op>` for every RFC 6902 operation in the patch (`patch:merge` for a merge patch),
and `profile:<name>`. An operation name outside RFC 6902 can only be refused, so it
needs just some `patch:*` capability. In strict mode an expected category needs `error-category` and an
expected position needs `error-position`. A case an adapter cannot do is reported as
unsupported, counted apart from passes and failures.

## Pins

`tests/pins.json` records the digest of the vendored data, of the corrections file
and of every own case directory. Any change, addition or removal of a case changes a
digest and fails the kit's own tests until the pins are regenerated
(`bin/kit pins --write`) and the diff is checked. Changing an expectation therefore
always shows in a diff of the pins.
