"""Adapters for the libraries that read text (str) and not bytes: yaml-rs, py-yaml12 and ryaml.

None of them has an event stream, reports a position, or names the kind of an error: they raise
one exception type with a message. So they offer `outcome:events` and `value`, and the kit
category is guessed from the message, which every error answer says (`classification`). They read
`str`, so the adapter decodes the bytes first (`decode`); that decoding is marked only when it
did more than a plain UTF-8 decode.
"""

from __future__ import annotations

import pykit
from pykit import Failure, ok

LIMIT_WORDS = ("limit", "too deep", "too many", "recursion", "nesting")


def category_of(message: str) -> str:
    m = message.lower()
    if "duplicate" in m:
        return "duplicate-key"
    if any(w in m for w in LIMIT_WORDS):
        return "limit"
    return "syntax"


def run(name: str, version: str, *, load_all, dump, own_errors, hook=None, info=None,
        accept=None) -> None:
    """`load_all(text) -> list of documents`, `dump(obj) -> str`, `own_errors` the exception
    types the library raises for a rejected input. `accept(text)` is the cheaper call that only
    has to say whether the library takes the text (the default is load_all)."""

    def classify(e):
        if isinstance(e, own_errors):
            return Failure(category_of(str(e)), str(e), emulated=("classification",))
        return None

    def text_of(data):
        text, changed = pykit.decode_text(data)
        return text, (("decode",) if changed else ())

    def load(data, parse=None):
        try:
            text, emu = text_of(data)
        except Failure as f:
            raise f from None
        try:
            return (parse or load_all)(text), emu
        except Exception as e:
            f = classify(e)
            if f is None:
                raise
            f.emulated += list(emu)
            raise f from None

    def events(data, args):
        _, emu = load(data, accept)
        raise pykit.Accepted(emulated=emu)

    def value(data, args):
        docs, emu = load(data)
        fields = {"value": pykit.JSONText(pykit.documents_json(docs, hook))}
        if emu:
            fields["emulated"] = list(emu)
        return ok(**fields)

    def no_bytes(_):
        # All three refuse a bytes value outright; none has a byte type to write as !!binary.
        raise pykit.Declined("the library refuses a bytes value: it has no byte type")

    def dumper(data, args):
        obj = pykit.from_kit(args["value"], binary=no_bytes)
        try:
            text = dump(obj)
        except (ValueError, TypeError) + tuple(own_errors) as e:
            raise Failure("other", f"{type(e).__name__}: {e}") from None
        return ok(output=pykit.b64(text.encode("utf-8")))

    hello = {
        "name": name, "version": version, "language": "python",
        "capabilities": ["outcome:events", "value", "dump", "error-category"],
        "emulates": ["classification", "decode", "error"],
        "info": {"python": pykit.python_version(), **(info or {})},
    }
    pykit.serve(hello, {"events": events, "value": value, "dump": dumper}, classify)
