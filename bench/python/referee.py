"""The reference reading of a dumped document.

Two parsers give the structure of the text, which is its nodes with their style, anchors, aliases
and explicit tags, and neither is asked what a plain scalar means:

* libyaml, through PyYAML's CSafeLoader events (a C parser that is a port of the original YAML 1.1
  reference parser), and
* ruamel.yaml's own parser events (a pure Python parser maintained for YAML 1.2, with its own
  scanner changes).

They are the two parsers in the Python ecosystem that expose events, which is what separates the
structure of a text from its meaning; the Rust-backed parsers return resolved values only. They do
share ancestry, so a case where they agree is read as the text's structure, and a case where they
differ or only one accepts the text is reported as `unjudged`, not as a verdict on the library.

What a plain scalar is comes from `resolve_core`, the harness's own resolver for the YAML 1.2 core
schema, and a merge key is an ordinary key. Both parsers are libraries that are also measured; here
they only read.
"""

from __future__ import annotations

import math
import re

from common import CompareError, docs_value, first_diff, key_string

_INT = re.compile(r"[-+]?[0-9]+")
_OCT = re.compile(r"0o[0-7]+")
_HEX = re.compile(r"0x[0-9a-fA-F]+")
_FLOAT = re.compile(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?")
_INF = re.compile(r"[-+]?\.(inf|Inf|INF)")
_NAN = re.compile(r"\.(nan|NaN|NAN)")

_NULLS = ("", "~", "null", "Null", "NULL")
_TRUES = ("true", "True", "TRUE")
_FALSES = ("false", "False", "FALSE")


def resolve_core(s: str):
    """A plain scalar by the YAML 1.2 core schema (section 10.3.2). `1_000` and `0b101` are text,
    `017` is the decimal 17, and `yes` is text."""
    if s in _NULLS:
        return None
    if s in _TRUES:
        return True
    if s in _FALSES:
        return False
    if _INT.fullmatch(s):
        return int(s)
    if _OCT.fullmatch(s):
        return int(s[2:], 8)
    if _HEX.fullmatch(s):
        return int(s[2:], 16)
    if _FLOAT.fullmatch(s):
        return float(s)
    if _INF.fullmatch(s):
        return -math.inf if s[0] == "-" else math.inf
    if _NAN.fullmatch(s):
        return math.nan
    return s


def core_tag(tag: str) -> str:
    """The short name of a core-schema tag in its long form, or ""."""
    p = "tag:yaml.org,2002:"
    return tag[len(p):] if tag.startswith(p) else ""


def resolve_tagged(tag: str, s: str):
    """A scalar that carries an explicit core-schema tag: the tag fixes the type, so `!!float 1` is the
    float 1.0 and `!!int x` is an error. Any other tag is one the reference reading does not know."""
    name = core_tag(tag)
    r = resolve_core(s)
    if name == "null":
        if r is None:
            return None
    elif name == "bool":
        if isinstance(r, bool):
            return r
    elif name == "int":
        if isinstance(r, int) and not isinstance(r, bool):
            return r
    elif name == "float":
        if isinstance(r, float):
            return r
        if isinstance(r, int) and not isinstance(r, bool):
            return float(r)
    elif name == "str":
        return s
    else:
        raise CompareError(f"explicit tag {tag}")
    raise CompareError(f"{s!r} is not a valid {tag}")


def scalar_value(tag, style, text):
    if tag == "!" or tag == "tag:yaml.org,2002:str":
        return text
    if tag is not None:
        return resolve_tagged(tag, text)
    if style:
        # libyaml reports a plain scalar's style as the empty string, ruamel.yaml as None.
        return text
    return resolve_core(text)


def docs_from_events(events) -> object:
    """The common form of a stream of parser events (both parsers' event classes share these names).
    Anchors are kept by name and an alias is the value its anchor had. Mappings are plain mappings:
    `<<` is an ordinary key. Iterative, so the nesting of the text cannot exhaust the stack."""
    docs: list = []
    anchors: dict = {}
    stack: list = []          # [container, pending key] per open collection
    root = [None]

    def put(v, anchor=None):
        if anchor:
            anchors[anchor] = v
        if not stack:
            root[0] = v
            return
        frame = stack[-1]
        c = frame[0]
        if isinstance(c, list):
            c.append(v)
        elif frame[1] is _NOKEY:
            frame[1] = key_string(v)
        else:
            c[frame[1]] = v
            frame[1] = _NOKEY

    for ev in events:
        n = type(ev).__name__
        if n == "ScalarEvent":
            put(scalar_value(ev.tag, ev.style, ev.value), ev.anchor)
        elif n == "AliasEvent":
            if ev.anchor not in anchors:
                raise CompareError(f"unknown alias *{ev.anchor}")
            put(anchors[ev.anchor])
        elif n in ("SequenceStartEvent", "MappingStartEvent"):
            c = [] if n == "SequenceStartEvent" else {}
            put(c, ev.anchor)
            stack.append([c, _NOKEY])
        elif n in ("SequenceEndEvent", "MappingEndEvent"):
            stack.pop()
        elif n == "DocumentStartEvent":
            anchors.clear()
            root[0] = None
        elif n == "DocumentEndEvent":
            docs.append(root[0])
    return docs_value(docs)


_NOKEY = object()


def read_libyaml(text: str):
    import yaml

    return docs_from_events(yaml.parse(text, Loader=yaml.CSafeLoader))


def read_ruamel(text: str):
    from ruamel.yaml import YAML

    return docs_from_events(YAML(typ="safe", pure=True).parse(text))


class RefereeError(Exception):
    def __init__(self, msg: str, rejected: bool = False):
        super().__init__(msg)
        self.rejected = rejected     # no reference parser accepts the text: it is not YAML


def two_referees(data: bytes):
    """The value both reference readings give the text. Raises RefereeError when they differ or when
    one of them (or both) refuses it."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RefereeError(f"not UTF-8: {e}", rejected=True)
    a = b = None
    ea = eb = None
    try:
        a = read_libyaml(text)
    except Exception as e:  # noqa: BLE001 - any refusal of the text, from either parser
        ea = e
    try:
        b = read_ruamel(text)
    except Exception as e:  # noqa: BLE001
        eb = e
    if ea is None and eb is None:
        d = first_diff(a, b)
        if d:
            raise RefereeError("the two reference parsers disagree " + d)
        return a
    if ea is not None and eb is not None:
        raise RefereeError(f"libyaml: {_first_line(ea)}; ruamel.yaml: {_first_line(eb)}", rejected=True)
    if ea is not None:
        raise RefereeError(f"libyaml rejects it, ruamel.yaml reads it: {_first_line(ea)}")
    raise RefereeError(f"ruamel.yaml rejects it, libyaml reads it: {_first_line(eb)}")


def _first_line(e: Exception) -> str:
    lines = str(e).strip().splitlines()
    return f"{type(e).__name__}: {lines[0] if lines else ''}"
