"""The behaviours an adapter may supply on a library's behalf.

An adapter declares the ones it uses in its hello (`emulates`) and names, in each answer,
the ones that answer depended on. The runner refuses any name that is not listed here, so
the same word means the same thing in every language and a report can be read across
libraries. Adding a name is a change to this table and to docs/adapter-protocol.md.
"""

from __future__ import annotations

EMULATES: dict[str, str] = {
    "unchanged": "The original bytes are reported after a failed patch, because the library "
                 "applies one edit at a time or returns nothing when it fails.",
    "error": "An error was raised by the adapter, not the library: a missing path, a "
             "malformed pointer, an unknown operation, a write through an alias, a missing "
             "document.",
    "pointer": "A JSON Pointer was resolved to a node (or its source span) by the adapter, "
               "because the library has no addressing of its own.",
    "decode": "Text encoding was handled by the adapter (detecting UTF-8, UTF-16 or UTF-32, "
              "removing a byte order mark, rejecting malformed input) because the library "
              "reads strings.",
    "style": "Scalar style, flow or block form or explicit document markers were recovered "
             "by the adapter from another view of the input (for example a tokenizer), "
             "because the library's own events do not carry them.",
    "tag": "A full tag was written out by the adapter from the handle and suffix the "
           "library gives.",
    "source-text": "The YAML text of a new value (style, block header, indentation, "
                   "quoting) was written by the adapter, because the library only swaps raw "
                   "text.",
    "classification": "The adapter mapped a failure the library reports only as a message to "
                      "a kit error category.",
}


def check(names) -> list[str]:
    """The names in `names` that are not in the vocabulary (empty when all are known)."""
    if not isinstance(names, (list, tuple, set, frozenset)):
        return ["<not a list>"]
    return sorted(str(n) for n in names if n not in EMULATES)
