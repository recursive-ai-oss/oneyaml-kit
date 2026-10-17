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
| `category` | `parse`, `schema`, `edit`, `diagnostics`, `safety`, `compat`, `roundtrip` or `emit`. |
| `origin` | `spec`, `bug`, `fuzz`, `real-world`, `competitor` or `upstream`. |
| `productions` | YAML 1.2.2 production numbers the case exercises, as strings. Optional; used by a future spec-coverage report. |
| `refs` | Citations: spec sections, RFC sections, or a page of these docs. A `spec` case must have at least one. |
| `tags` | Free labels. `slow` keeps a case out of the fast mode. `convention` marks a case whose exact bytes rest on a rule the kit chose where the specification leaves a choice (listed in [edit-semantics.md](edit-semantics.md)); `strict-convention` marks an `emit` case whose loose verdict is the specification but whose strict verdict adds the family's writing convention; `comment-ownership` is the more specific label for the rules about which comments go with a removed entry, and such a case carries both. A case whose notes call something a convention or open carries `convention`. Tags are copied into result records and reports count the cases apart, so a library is not failed on taste unnoticed. |
| `notes` | Optional reasoning for a reader. |
| `provenance` | Where the case comes from: optional `source` (URL), `revision`, `licence` and `reference` (a report, issue or paper), all non-empty strings. A `bug`, `fuzz` or `competitor` case must have `reference`; a `real-world` case must have `source` and `licence`. A case written for the kit says so in `reference`. |
| `input` | Input file name; default `input.yaml`. A `dump` case has no input. |
| `input_recipe` | Instead of an input file: a list of parts that describe the input bytes, for an input too large to store. See *Generated inputs*. A case has an input file or a recipe, not both. |
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
| `dump` | none: the operation holds a `value` | the text the library writes for the value, with its default settings |

`patch` takes `patch` (an RFC 6902 array, or an RFC 7396 document when
`format` is `merge-patch`), `format` (`json-patch`, the default, or `merge-patch`)
and `document` (index of the document in the stream, default 0). Pointers are RFC
6901. A patch is atomic: when any operation fails the document is unchanged.

`dump` takes `value`, a kit value (below) that is one document: the library writes it, and
the kit reads the text back (see *Written values*). Integers beyond 64 bits and mappings whose keys are not strings (the `$map` form) are allowed. A library with no such value of its own (no integer beyond 64 bits, no mapping with keys that are not strings) answers unsupported; an adapter passes them through the library's own type when it has one.

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
- A byte string is the tagged node `{"$tagged": {"tag": "tag:yaml.org,2002:binary", "value": "<base64>"}}`:
  the Core schema does not resolve the tag, so the node is a tagged string whose text is the
  base64 of the bytes (the YAML 1.1 binary type, RFC 4648 section 4: standard alphabet, padded).
  Only `emit` cases use it.
- A value has no cycles. A YAML graph in which an alias refers to a node that contains it is
  valid in the data model, but it has no kit value form, so a `value` operation on it is an
  error (any category), and a library must never answer with a finite value instead. This is the
  kit's rule for its value form, not a rule of the specification, and such cases carry `convention`.

An upstream value comes from yaml-test-suite's `in.json`, which has no tags and was
produced by a converter that loses the difference between integers and floats
(`450.00` is `450`). Those cases compare with `tags: ignore`, a named projection that
unwraps `$tagged` on both sides and nothing else. Number typing stays exact, so a
library that reads `450.00` as an integer fails; where upstream is wrong about a
number, the correction is recorded (see below). Own cases cannot use `tags: ignore`.

Values have no depth limit: loading, validating and comparing walk with an explicit stack,
so a case can hold a value nested a hundred thousand levels deep, and an adapter can answer
with one.

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
- When the input is not validly encoded, the position is that of the first byte of
  the ill-formed sequence; its column counts the characters decoded before it, plus
  one. A sequence that is merely incomplete at that byte is not counted as a
  character.

## Expectations

| `kind` | Fields | Passes when |
|---|---|---|
| `events` | `file` (event lines) or `recipe` (see *Generated inputs*) | no error, and the event lines are equal |
| `value` | `value` (list of documents) or `file` (JSON) | no error, and the documents are equal |
| `bytes` | `file` or `recipe` (see *Generated inputs*) | no error, and every output byte is equal |
| `error` | `category`, `line`, `column`, `events_file`, `unchanged` | an error is reported (see below) |
| `bounded` | `time_seconds`, `memory_mb`, `accept`, `error`, `unchanged` | see below |
| `readback` | none (`dump` operations only) | the written text reads back as the value, see *Written values* |

For `error`, **loose** mode only requires that an error is reported, of any kind,
anywhere. **Strict** mode also checks whatever the case states:

- `category`: one of `syntax`, `encoding`, `limit`, `patch-path`, `patch-test`,
  `patch-invalid`, `refused-alias`, `refused-ambiguous`, `unresolved-tag` (a reader
  refused a tag it cannot resolve, YAML 1.2.2 section 3.3), `limit` (input or a value beyond
  what the library will hold or spend: depth, size, aliases, digits), `duplicate-key` (a mapping
  holds two equal keys, which YAML 1.2.2 section 3.2.1.1 forbids: the keys of a mapping are
  unique), or `any` (the default).
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
"expect": {"kind": "bounded", "time_seconds": 100, "memory_mb": 1024, "error": "limit",
           "accept": {"kind": "events", "file": "expected.events"}}
```

- `accept` (optional) is an exact result of the operation's own kind. If the library
  answers, the whole result must equal it. A wrong or truncated result is a failure.
- `error` (optional) is a refusal that is acceptable: a category, or a list of distinct
  categories when more than one kind of error is correct (for example unclosed deep input,
  which is a syntax error or a limit error depending on where the reader stops):
  `"error": ["syntax", "limit"]`. `any` is not allowed here. Loose accepts any error; strict
  needs the adapter's `error-category` and checks that the category is one of the list.
- At least one of the two is required. A case with only `error` allows no result; a
  case with only `accept` allows no refusal.
- `unchanged` (optional, `patch` operations with an `error` only) states that a refused patch
  leaves the document as it was, which a patch must (RFC 6902 section 5). When the library
  refuses, the document it reports must be byte for byte the input; one that reports another
  document, or none, fails strict, the same as for an `error` expectation (loose asks only that
  an error is reported). It is judged on the refusal alone, so a library that answers with a
  result, goes over a bound or crashes is judged as before. Not stated, nothing is checked.
- `time_seconds` and `memory_mb` are part of the case. The runner enforces them
  whatever the adapter manifest says: the case is cut off at the time bound, and the
  adapter process is killed if its resident memory grows past the memory bound while the
  case runs. Memory is the growth in peak resident memory over what the process held
  when the case started. Every bounded case runs in an adapter process of its own, started for
  that case and ended after it, so "what the process held" is the same fresh start every time.
  Without that, the number depends on the cases that ran before: a runtime keeps memory it has
  freed (a collector's heap, buffers a parser reuses), so a case that grows a fresh process by
  hundreds of megabytes can show half that, or nothing, after a case that already grew it.
  Ordinary cases share one process as before. Time is measured from the case's request to its
  answer. The process first answers one trivial request of the same kind (the text `a` and a line
  break, and an empty patch for a patch case), so first-use costs, such as compiling code just in
  time or loading types, are not counted against the case's bounds.
- The time bound is a hang guard, not a speed claim, and it is generous by design. It is set
  far above the time any passing reader was measured to need, including on one or two cores; a
  reader that passes close to its bound would make the verdict depend on the machine, and the case
  is then corrected. A hang or a runaway cost trips it; a slow or busy
  machine does not, and a reader that is slow but finishes inside it passes. It does not separate
  readers by cost. The memory bound is the real check of a bounded case. The runner enforces the
  time number as written, with no scaling for the machine, and each result records the bound that
  was enforced (`measured.time_bound`). A bounded case states at least five seconds.
- A case that exists to catch cost that grows faster than the input is sized so that the guard
  still has force: the work a reader with that cost must do, counted in bytes walked or copied,
  takes longer than the guard even at the speed of a simple byte-at-a-time loop or a plain memory copy that does nothing else (measured on one machine, not a bound). The kit's own
  tests check that arithmetic from the case data and time nothing, so a case cannot be shrunk
  below it unnoticed. Where a case has to stay in the fast set, its input is a stored file and is
  too small for this; a larger sibling in the slow set carries the cost check and the case says so.
- Loose passes when the outcome is the accepted result or any error, inside both bounds.
  Strict also needs the named category. If memory cannot be measured on the platform
  (see the adapter protocol: the whole process tree is measured), the memory bound is not
  checked in loose mode, and strict is unsupported rather than a silent pass. The time
  bound is always checked.
- Results record what came back (`got`) and the measured time and memory.
- A library that took the input but cannot describe the result answers `accepted` (adapter
  protocol). The kit grades that from the measured cost: over a bound is a failure; within
  the bounds it is a failure when only an error is allowed, and unsupported when a result
  was also allowed, because the result was not described.

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

## Written values

An `emit` case gives a value and asks the library to write it. The layout is the library's
choice, so the expectation is not a set of bytes: the kit reads the text back with its own
reader (`tools/oneyaml_kit/readback.py`) and compares the result with the value. The reader
is independent of every library, because a library that reads its own output agrees with its
own mistakes, and it follows the rules of YAML 1.2.2 for the constructs a writer uses: block
and flow collections, plain, quoted and block scalars, `? ` keys, comments and one document.
Anchors, aliases, custom tags, tag directives and several documents are refused by name,
because a written value has no shared nodes; so is a raw character that is not printable. It
is checked against yaml-test-suite: it reads the same values as the suite records for more than
200 valid documents, and refuses all 94 invalid ones. It is a judge of what a writer wrote, not a
validator, so only the suite's invalid documents are promised.

The text is read three ways, and the verdicts are split like this:

* **Loose**: the text is valid YAML and means the value under the YAML 1.2 Core schema. The
  value must be equal in full: types (`1` and `1.0` and `"1"` are three values), strings,
  order of sequences, keys of mappings. An implicit key (a key without `? `) must be on one
  line and at most 1024 characters long, counting its quotes; a lenient reader accepts a
  longer one, so the kit checks the length itself. A parse error is a failure.
* **Strict** adds the family's writing convention, which is a rule the family chose and not
  a requirement of the specification: the text must also mean the value under YAML 1.1
  (`yes`, `on`, `0755`, `1_000`, `1:30`, timestamps, `<<` and `=` are not strings there; a
  float needs a point and a signed exponent: `1.0e+17`, not `1E+17`), and a plain string must
  not have one of the listed number forms that some common readers accept although neither
  schema reads it as a number: a
  `0x`, `0o` or `0b` prefix in any case with a sign before or after it, a number after two
  or more signs, a number once every `_` is removed. NEL, LS, PS and a byte order mark
  are written as escapes, because YAML 1.1 reads the first three as line breaks.

A problem under the convention only is therefore loose pass, strict fail. The loose verdict is
the specification. A raw control character, a raw byte order mark outside a quoted scalar, a
string written as a number, or a float written as an integer fails it. Only the strict layer is
the family convention, and the `emit` cases where the two can differ carry the
`strict-convention` tag, so reports count them apart. The reader's forms are listed in
the module; it follows the YAML 1.1 type pages (`yaml.org/type`) and the number syntax that
PyYAML, gopkg.in/yaml.v3 and goccy/go-yaml accept.

A byte string is judged by its bytes. The reader takes a scalar tagged `!!binary` (or with the
verbatim tag `!<tag:yaml.org,2002:binary>`), strips the white space and line breaks that a writer
folds the base64 with (space, tab, CR and LF only), and requires padded base64 of the standard
alphabet; the bytes it decodes to must be the case's bytes, whatever the spelling of the text.
Text with no tag, a list of numbers or a different tag is a different value. Loose and strict
judge this the same way, because the tag is the only way to tell bytes from text and the YAML 1.1
binary type is its only definition.

The verdict for a library follows from what its own byte-string route writes:

- it writes the bytes as text that does not read back as those bytes (untagged base64, say):
  **fail**;
- its own data model writes a byte array as something else, a list of integers for instance:
  **unsupported**, with that as the reason, because the case asks for a type the library does not
  have;
- it has no byte type at all: **unsupported**.

The adapter never wraps the bytes in a tag, a base64 string or a list itself.

An adapter answers with `output` (base64 of the bytes written); an `error` or a missing
output is a failure, and `unsupported` is a declined case that must be listed as a known
deviation.

## Capabilities a case needs

The runner works out what an adapter must support from the case: `events`, `value`, `dump`,
`patch:<op>` for every RFC 6902 operation in the patch (`patch:merge` for a merge patch),
and `profile:<name>`. An operation name outside RFC 6902 can only be refused, so it
needs just some `patch:*` capability, and so does an empty patch (the way a round trip is
asked for: the document must come back byte for byte). In strict mode an expected category needs `error-category` and an
expected position needs `error-position`. A case an adapter cannot do is reported as
unsupported, counted apart from passes and failures.

## Generated inputs

A stored file may not be larger than 1 MB, so an input that is megabytes long is described by a
recipe in `case.json` and built when the case is loaded. A recipe is data: nothing in it is
evaluated, no text is substituted, and no file or program is read.

```json
"input_recipe": [
  {"text": "{", "repeat": 9000},
  {"numbers": {"count": 140608, "prefix": "k", "suffix": ": v,\n", "width": 6}},
  {"text": "}\n"}
]
```

| Part | Builds |
|---|---|
| `{"text": s, "repeat": n}` | the UTF-8 bytes of `s`, `n` times (`repeat` defaults to 1) |
| `{"numbers": {"count": n, "start": k, "prefix": p, "suffix": s, "width": w}}` | `p`, the decimal number, `s` for `k`, `k+1`, ... `k+n-1`, the number zero-padded to at least `w` digits. `start`, `prefix`, `suffix` and `width` are optional. Used for keys that must all be different. |
| `{"product": {"prefix": p, "suffix": s, "choices": [[a, b], [c, d, ...], ...]}}` | `p`, one string from each list in turn, `s`, for every combination of one string from each list, the first list varying slowest and the last fastest. `choices` is required and holds at least one list; `prefix` and `suffix` are optional; every list holds at least one string, and all strings are written as UTF-8. Used for names built from independent parts, such as a large set of names with one hash. |

A recipe has at most 64 parts and builds at most 64 MiB; the size (for a product, the number of combinations and the exact length of every one) is worked out from the parts
before a byte is built, so a recipe that is too large is refused when the case loads. The parts
are written one after another; counts and repeats are integers of 0 or more.

An expected result may use a recipe in the same way, for a result that is as large as the input
or larger: `{"kind": "events", "recipe": [...]}` builds the text of an events file, lines ending
in `\n`, and `{"kind": "bytes", "recipe": [...]}` builds the exact bytes a patch must produce.
Both also work inside `accept`. A result has a file or a recipe, never both, and the same limits
and parts apply as for an input; the error messages call it `recipe`. A patch whose output is
larger than a stored file may be (1 MB) is expressed this way. For example, an output of one
key, a value and a flow sequence of four thousand copies of that value:

```json
"accept": {"kind": "bytes", "recipe": [
  {"text": "a: 1\nv: <value>\nf: ["},
  {"text": "<value>, ", "repeat": 3999},
  {"text": "<value>]\n"}
]}
```

A JSON Patch of very many identical operations is too large for a stored file in the same way, so
`operation.patch_recipe` can stand in for `patch`: a list of at most 64 parts, each
`{"ops": [operations], "repeat": n}`, written one after another (`repeat` defaults to 1). It builds
at most 1,000,000 operations, counted before anything is built, and only for a `json-patch`; a case
has a `patch` or a `patch_recipe`, never both. The runner sends the built array like a stored one.
`tests/pins.json` pins the operation count and the SHA-256 of the built array (`generated_patches`).

The runner sends a generated input like a stored one: the bytes, base64, in the request. Nothing
is written to disk. `tests/pins.json` pins the size and the SHA-256 of every generated input
(`generated_inputs`) and of every expected result built from a recipe (`generated_results`),
besides the digest of the case directory that holds the recipe, so a change to a recipe, or to
what a recipe builds, fails the kit's tests until the pins are regenerated and the diff is
checked.

The large generated cases carry the tag `slow`: a case whose input, or whose expected result,
is larger than 1 MB must, and so should a case that makes a competing library run to its full time limit
(the limit is a hang guard of a hundred seconds or so). A run leaves them out, without building their
inputs or expected results, unless `--slow` is given or the environment variable `KIT_SLOW` is
set (to anything but empty or `0`). The full gate sets it, so the large cases run there and not in
the hand-over tier. A run without them skips deviation entries that name them instead of calling
them stale; an entry for an id that is no case is still an error. Inputs are built in pieces of
about a megabyte (a piece ends at the first line that reaches a megabyte, so a product of very long lines has pieces as long as a few lines), so building one holds little more than its own bytes.

## Pins

`tests/pins.json` records the digest of the vendored data, of the corrections file
and of every own case directory. Any change, addition or removal of a case changes a
digest and fails the kit's own tests until the pins are regenerated
(`bin/kit pins --write`) and the diff is checked. Changing an expectation therefore
always shows in a diff of the pins.
