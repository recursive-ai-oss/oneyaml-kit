# Edit semantics

The kit's edit cases are patches with the exact bytes expected afterwards. These are
the rules those expectations follow.

## Minimal diff

Reading a document, changing one value and writing it back changes only that value's
text. Everything else stays byte for byte: comments, blank lines, indentation, line
endings, a byte order mark, the spelling and style of every other node.

## The value keeps its style

A replaced value keeps its own style (plain, single or double quoted, literal or
folded block, flow or block collection) as long as that style can hold the new value.

## Setters are exact

Reading the edited document back always gives exactly the value that was set. A
literal block scalar with the default clip chomping holds a value ending in exactly one
newline; set to text with no trailing newline, the header must change to the strip
form `|-`, and nothing else changes:

```yaml
b: |
  this is a test
```

set to `"this is a test2\n"` gives `b: |` followed by `  this is a test2`; set to
`"this is a test2"` gives `b: |-` followed by `  this is a test2`. The cases
`edit/block-literal-keeps-style-with-newline` and
`edit/block-literal-strip-when-no-trailing-newline` pin both.

## Kit conventions

Some of what an edit writes is not decided by YAML 1.2.2, RFC 6902 or RFC 7396 but still
has to be one thing for the cases to state exact bytes. These are the kit's rules, shared
by every library in the family. They are conventions, not facts about the specification,
and every case whose bytes rest on one carries the tag `convention`; a report counts those
cases apart, so a library that chose differently is shown as having done so and not as
having got YAML wrong.

- **Where a new mapping member goes.** At the end of its mapping. A new sequence item goes
  at its index. RFC 6902 leaves the position of a member of an object unspecified.
- **Strip becomes clip when the value gains exactly one final line break.** A literal or
  folded scalar moves between clip (no indicator), strip (`-`) and keep (`+`) only as the
  value's final line breaks require: none gives strip, exactly one gives clip, more give
  keep. Reading the result back gives exactly the value in every case; the convention is
  that the header changes and nothing else does.
- **Inserting into a flow collection.** A value added to an empty or one-pair flow
  collection is written `[x]` and `{k: v}`, and later members are separated by `, `.
- **The quote of a string that cannot stay plain.** A plain scalar that would read back as
  another type, as a comment, as a key, or lose its edge spaces becomes single-quoted. The
  specification allows any quoted style; single quotes need no escapes.
- **Comments of a removed entry.** See *Comments* below.
- **The layout of a new nested sequence** copies the indentation of a sibling sequence.
- **Indentation of a rewritten block scalar** reuses the old content's indentation.
- **A multi-line string replacing an inline scalar is double-quoted.** A plain or quoted
  scalar that is set to text with a line break keeps its place on the key's line and is
  written in double quotes with `\n` escapes. The specification also allows single quotes
  with blank lines and block scalars; the family writes double quotes.
- **An anchor stays on a replaced node**, including while aliases use it, so the aliases
  now read the new value. The new value is separated from the anchor by a space.
- **A tag on a replaced node stays only if reading the new value back under that tag gives
  exactly the value that was set**; otherwise it is dropped. A custom tag on a scalar stays
  while the new value is a scalar; `!`, `!!binary` and the like go when the new value is, say,
  an integer.
- **An empty key is addressed by the empty pointer token.** A token matches a key by its
  scalar text, and a key with no content has the empty text.
- **A value for an explicit key that has none** is written on a new line at the key's indentation, as `: value`, after the key line and its comment; a missing final line break stays missing.

## The value keeps its style, and what follows from exactness

Where the existing style can hold the new value, it stays. A block scalar's header
changes only as far as the value needs. The following are consequences of exact read-back,
not conventions:

- an integer, float, boolean or null cannot be written as a quoted or block scalar, so
  the quotes or the header go and the value is written plain;
- a keep scalar set to one final line break loses the empty line the old value ended
  with, because left in place it would be part of the new value;
- an indentation indicator that is still needed stays;
- in a folded scalar, one line break between lines of text is written as an empty line,
  because a single break folds to a space;
- a double-quoted scalar escapes what it must (`\"`, `\\`, `\n`), and a single-quoted one
  doubles its quote;
- removing the last entry of a block mapping or the last item of a block sequence that is
  nested in another node leaves `{}` or `[]` (`a: {}`), never an empty value, which would
  read back as null.

What happens to a tag or an anchor on a node that is copied is not pinned by the cases
beyond what a case states.

## Line breaks and encoding

New lines use the document's own line break (LF, CRLF or lone CR), which follows from
writing in the style of the surrounding text. A byte order mark and the document's
encoding (UTF-8, UTF-16, UTF-32) are kept.

## Inserted content copies its siblings

A value added by `add` (or the target of `copy` and `move`) is written in the style of
its siblings: block or flow, indentation, quoting. A value that is not a string is never
quoted, whatever its siblings do.

A copied alias whose anchor would not precede the destination is written out as the value it stands for, because an alias may not come before its anchor. The bytes a patch copies, including aliases written out, count against the alias-expansion byte budget, and a patch that goes over it fails with a limit error and changes nothing.

## Refusals

A refused edit changes nothing and reports an error with a stable category:

- a write through an alias (`refused-alias`), because it would silently change every
  other place that shares the anchored node. Replacing the alias node itself is fine,
  and reading follows aliases;
- a pointer token that matches two keys, such as `80` and `"80"` (`refused-ambiguous`);
  the edit never guesses.

A pointer token matches a mapping key by the key's scalar text. Mappings or sequences
used as keys cannot be addressed by a pointer.

## Comments

Edits to a value do not touch comments outside the value. A comment inside a collection
that is replaced or removed goes with it.

When an entry is removed, the kit's convention (tag `comment-ownership`, as well as
`convention`) is that a comment at the end of its line, a comment on the line directly
above it with no blank line between, and a comment inside its value go with it; a comment
directly above the next entry, a comment after the last entry, and a header comment
separated from the first entry by a blank line stay. YAML defines comments as presentation
detail with no owner, so these cases are few.

## The `test` operation

`test` compares the value at the path with the patch's value by the YAML data model under
the core schema (YAML 1.2.2 section 3.2.1.3, node comparison, with the schema's types):
two scalars are equal when they have the same type and the same value, mappings are
equal regardless of key order, and sequences are equal only in order. A plain, quoted or
block scalar with the same text is the same string; `0x1F` and `31` are the same integer;
`yes` is a string, not a boolean.

A number in a patch maps to a YAML type by how JSON writes it, the rule of the kit's
values (see the case format): a number without a decimal point or exponent is an integer,
one with either is a float. So `1` is the integer 1 and `1.0` is the float 1.0, and they
are **not** equal: testing the document's `1` against `1.0`, or its `1.0` against `1`,
fails with `patch-test`. This departs from RFC 6902 section 4.6, which compares numbers
numerically (1 equals 1.0). It is a kit convention (tag `convention`), because YAML 1.2
gives the two different types and a patch that tests a type should be able to say so. A
library that compares numerically, as the RFC does, fails these cases and lists them as
known deviations with that reason.

## Atomicity

A patch is applied as a whole. If any operation fails, including a failing `test`,
the document is unchanged (`unchanged` in the case's expectation).

## Multiple documents

A patch addresses one document of a stream, selected by index. The pointer syntax
stays that of RFC 6901.
