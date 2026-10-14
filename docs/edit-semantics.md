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

## Inserted content copies its siblings

A value added by `add` (or the target of `copy` and `move`) is written in the style of
its siblings: block or flow, indentation, quoting.

## Refusals

A refused edit changes nothing and reports an error with a stable category:

- a write through an alias (`refused-alias`), because it would silently change every
  other place that shares the anchored node. Replacing the alias node itself is fine,
  and reading follows aliases;
- a pointer token that matches two keys, such as `80` and `"80"` (`refused-ambiguous`);
  the edit never guesses.

A pointer token matches a mapping key by the key's scalar text. Mappings or sequences
used as keys cannot be addressed by a pointer.

## Atomicity

A patch is applied as a whole. If any operation fails, including a failing `test`,
the document is unchanged (`unchanged` in the case's expectation).

## Multiple documents

A patch addresses one document of a stream, selected by index. The pointer syntax
stays that of RFC 6901.
