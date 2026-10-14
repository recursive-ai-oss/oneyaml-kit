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

## Nearest style that works

When the existing style cannot hold the new value, the nearest style that can is used,
for example a plain scalar that would read back as a different type becomes quoted.
Only that value's text changes. Cases state exact bytes only where one style is the
clear nearest choice.

Where the existing style can hold the new value, it stays, and a block scalar's header
changes only as far as the value needs:

- an integer, float, boolean or null cannot be written as a quoted or block scalar, so
  the quotes or the header go and the value is written plain;
- a literal or folded scalar moves between clip (no indicator), strip (`-`) and keep
  (`+`) only when the value's final line breaks require it: none gives strip, exactly
  one gives clip, more give keep. A keep scalar set to one final line break stays keep
  and loses the empty line the old value ended with;
- an indentation indicator that is still needed stays;
- in a folded scalar, one line break between lines of text is written as an empty line,
  because a single break folds to a space;
- a double-quoted scalar escapes what it must (`\"`, `\\`, `\n`), and a single-quoted one
  doubles its quote.

When a plain scalar cannot hold a string (it would read back as another type, as a
comment, as a key, or lose its edge spaces) it becomes quoted. Which quote is the nearest
is not fixed by the specification; the cases that state it use single quotes and carry
the tag `judgement`.

## Collections left empty

Removing the last entry of a block mapping or the last item of a block sequence that is
nested in another node leaves `{}` or `[]` (`a: {}`), never an empty value, which would
read back as null.

## Line breaks and encoding

New lines use the document's own line break (LF, CRLF or lone CR). A byte order mark and
the document's encoding (UTF-8, UTF-16, UTF-32) are kept.

## Inserted content copies its siblings

A value added by `add` (or the target of `copy` and `move`) is written in the style of
its siblings: block or flow, indentation, quoting. A new mapping key goes at the end of its
mapping and a new item at its index; the specification leaves the position of a member
open. A value that is not a string is never quoted, whatever its siblings do.

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

Edits to a value never touch comments. When an entry is removed, a comment on its own
line, a comment inside its value and a comment at the end of its line go with it; a
comment about the next entry, a comment after the last entry and a header comment
separated by a blank line stay. The specification does not define comment ownership, so
the cases that depend on it carry the tag `comment-ownership` and are few.

## Atomicity

A patch is applied as a whole. If any operation fails, including a failing `test`,
the document is unchanged (`unchanged` in the case's expectation).

## Multiple documents

A patch addresses one document of a stream, selected by index. The pointer syntax
stays that of RFC 6901.
