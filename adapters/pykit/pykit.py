"""What the Python adapters share: the JSON Lines loop, base64, the kit's value notation,
the encoding detection a library that reads text needs, and event lines.

Standard library only. An adapter module supplies its hello and a handler per operation; this
module answers the protocol (docs/adapter-protocol.md) around them.

Nothing here knows a YAML library. A library's exceptions are turned into `Failure` by the
adapter's own `classify` function; an exception it does not recognise is answered `crash`, never
as a rejection of the input.
"""

from __future__ import annotations

import base64
import datetime
import json
import re
import sys

PROTOCOL = 2
BINARY_TAG = "tag:yaml.org,2002:binary"
# A shared graph can expand to more nodes than any answer can hold (an alias bomb that a library
# loaded without copying). Past this many nodes the adapter reports "accepted" instead.
MAX_NODES = 2_000_000


class Declined(Exception):
    """The adapter cannot translate this case; answered `unsupported` with the reason."""


class Accepted(Exception):
    """The library took the input and the result cannot be described; answered `accepted`."""

    def __init__(self, emulated=()):
        super().__init__()
        self.emulated = list(emulated)


class Failure(Exception):
    """The input (or patch) was refused; answered `error`."""

    def __init__(self, category="other", message="", line=None, column=None, events=None,
                 output=None, emulated=()):
        super().__init__(message)
        self.category = category
        self.message = message
        self.line = line
        self.column = column
        self.events = events
        self.output = output
        self.emulated = list(emulated)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def ok(**fields) -> dict:
    fields["status"] = "ok"
    return fields


# ---------------------------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------------------------

_ID = re.compile(rb'"id"\s*:\s*("(?:[^"\\]|\\.)*")')


class JSONText(str):
    """JSON text that is written into the answer as it is (a value too large to build twice)."""

    __slots__ = ()


_SPLICE = "splice-marker"


def python_version() -> str:
    return "%d.%d.%d" % sys.version_info[:3]


def _write(out, obj) -> None:
    raw = obj.get("value")
    if type(raw) is JSONText:
        obj = {**obj, "value": _SPLICE}
    text = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
    if type(raw) is JSONText:
        text = text.replace(json.dumps(_SPLICE), raw, 1)
    out.write(text.encode("ascii") + b"\n")
    out.flush()


def serve(hello: dict, handlers: dict, classify=None, stdin=None, stdout=None) -> None:
    """Answer requests until `bye` or end of input.

    `handlers` maps an operation name to `fn(data: bytes, args: dict) -> dict`, where the dict is
    the response without `id`. A handler may raise Declined, Accepted or Failure. `classify`
    turns the library's own exception into a Failure (or returns None for "not one of its
    errors", which makes the answer `crash`).
    """
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except (ValueError, RecursionError):
            m = _ID.search(line[:4096])
            if m:
                _write(stdout, {"id": json.loads(m.group(1)), "status": "unsupported",
                                "reason": "the request cannot be read by json.loads (nesting too deep)"})
            continue
        kind = req.get("type")
        if kind == "hello":
            _write(stdout, {"type": "hello", "protocol": PROTOCOL, **hello})
            continue
        if kind == "bye":
            return
        if kind != "run":
            continue
        _write(stdout, {"id": req.get("id"), **answer(req, handlers, classify)})


def answer(req: dict, handlers: dict, classify) -> dict:
    handler = handlers.get(req.get("op"))
    if handler is None:
        return {"status": "unsupported", "reason": f"operation {req.get('op')!r} is not offered"}
    try:
        data = base64.b64decode(req.get("input") or "")
        return handler(data, req.get("args") or {})
    except Declined as e:
        return {"status": "unsupported", "reason": str(e) or "declined"}
    except Accepted as a:
        return {"status": "accepted", **({"emulated": sorted(set(a.emulated))} if a.emulated else {})}
    except Failure as f:
        return failure_response(f)
    except Exception as e:  # noqa: BLE001 - the library's own errors are mapped below; the rest is a crash
        f = classify(e) if classify else None
        if f is not None:
            return failure_response(f)
        return {"status": "crash", "detail": f"{type(e).__name__}: {str(e)[:200]}"}


def failure_response(f: Failure) -> dict:
    err = {"category": f.category, "message": (f.message or "")[:500]}
    if f.line is not None:
        err["line"] = f.line
    if f.column is not None:
        err["column"] = f.column
    r = {"status": "error", "error": err}
    if f.events is not None:
        r["events"] = f.events
    if f.output is not None:
        r["output"] = b64(f.output)
    if f.emulated:
        r["emulated"] = sorted(set(f.emulated))
    return r


# ---------------------------------------------------------------------------------------------
# Text decoding for libraries that read text and not bytes
# ---------------------------------------------------------------------------------------------

_BOMS = ((b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
         (b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))


def detect_encoding(data: bytes) -> tuple[str, int]:
    """The encoding of a YAML stream and the length of its byte order mark (YAML 1.2.2 5.2)."""
    for bom, enc in _BOMS:
        if data.startswith(bom):
            return enc, len(bom)
    h = data[:4]
    if len(h) == 4:
        if h[0] == 0 and h[1] == 0 and h[2] == 0 and h[3] != 0:
            return "utf-32-be", 0
        if h[0] != 0 and h[1] == 0 and h[2] == 0 and h[3] == 0:
            return "utf-32-le", 0
    if len(h) >= 2:
        if h[0] == 0 and h[1] != 0:
            return "utf-16-be", 0
        if h[0] != 0 and h[1] == 0:
            return "utf-16-le", 0
    return "utf-8", 0


def line_column(text: str) -> tuple[int, int]:
    """The 1-based position just after `text` (breaks: LF, CR LF, lone CR)."""
    breaks = re.findall(r"\r\n|\r|\n", text)
    line = 1 + len(breaks)
    if not breaks:
        return 1, len(text) + 1
    tail = re.split(r"\r\n|\r|\n", text)[-1]
    return line, len(tail) + 1


def decode_text(data: bytes) -> tuple[str, bool]:
    """Decode a stream for a library that only reads text. Returns the text and whether the
    adapter did more than a plain UTF-8 decode (a mark removed, UTF-16 or UTF-32 recoded), which
    the answer then marks as `decode`. A malformed stream raises an encoding Failure."""
    enc, skip = detect_encoding(data)
    body = data[skip:]
    try:
        text = body.decode(enc)
    except UnicodeDecodeError as e:
        line, col = line_column(body[: e.start].decode(enc, errors="ignore"))
        raise Failure("encoding", f"{enc}: {e.reason}", line, col,
                      emulated=("decode", "error")) from None
    return text, (enc != "utf-8" or skip > 0)


# ---------------------------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------------------------

_ESC = str.maketrans({"\\": "\\\\", "\0": "\\0", "\b": "\\b", "\n": "\\n", "\r": "\\r", "\t": "\\t"})
_STYLE = {None: ":", "": ":", "'": "'", '"': '"', "|": "|", ">": ">"}


def event_line(ev, tag_of) -> str | None:
    """One line of the event notation for a PyYAML-shaped event object, or None for the
    stream start and end. `tag_of(ev)` gives the full tag written in the document, or None."""
    n = type(ev).__name__
    if n in ("StreamStartEvent", "StreamEndEvent"):
        return "+STR" if n == "StreamStartEvent" else "-STR"
    if n == "DocumentStartEvent":
        return "+DOC ---" if ev.explicit else "+DOC"
    if n == "DocumentEndEvent":
        return "-DOC ..." if ev.explicit else "-DOC"
    if n == "AliasEvent":
        return f"=ALI *{ev.anchor}"
    parts = []
    if n in ("MappingStartEvent", "SequenceStartEvent"):
        parts.append("+MAP" if n == "MappingStartEvent" else "+SEQ")
        if ev.flow_style:
            parts.append("{}" if n == "MappingStartEvent" else "[]")
    elif n == "MappingEndEvent":
        return "-MAP"
    elif n == "SequenceEndEvent":
        return "-SEQ"
    elif n == "ScalarEvent":
        parts.append("=VAL")
    else:
        raise Declined(f"unknown event {n}")
    if ev.anchor:
        parts.append(f"&{ev.anchor}")
    tag = tag_of(ev)
    if tag:
        parts.append(f"<{tag}>")
    if n == "ScalarEvent":
        parts.append(_STYLE[ev.style] + ev.value.translate(_ESC))
    return " ".join(parts)


# ---------------------------------------------------------------------------------------------
# Kit values: library value -> JSON text
# ---------------------------------------------------------------------------------------------

class Tagged:
    """A node with a tag the schema does not resolve (hooks return this)."""

    __slots__ = ("tag", "value")

    def __init__(self, tag, value):
        self.tag = tag
        self.value = value


class _Lit(str):
    """Text already written in JSON; distinguishes it from a string value on the work stack."""

    __slots__ = ()


class _Exit:
    __slots__ = ("key",)

    def __init__(self, key):
        self.key = key


_dumps = json.dumps
_COMMA = _Lit(",")
_COLON = _Lit(":")


def _float_text(x: float) -> str:
    if x != x:
        return '{"$float":"nan"}'
    if x in (float("inf"), float("-inf")):
        return '{"$float":"%sinf"}' % ("-" if x < 0 else "")
    return float.__repr__(float(x))


def to_json(root, hook=None) -> str:
    """The kit value notation (JSON text) of a library's value, written with an explicit stack so
    depth is no limit. `hook(obj)` may return a replacement (another object, or Tagged) for an
    object of a library-specific type, or None. Raises Failure for a cyclic value (the notation
    has none), Accepted past MAX_NODES (a shared graph that would expand without end) and
    Declined for a type with no notation."""
    out: list[str] = []
    app = out.append
    work: list = [root]
    onpath: set[int] = set()
    nodes = 0
    pop = work.pop
    push = work.append
    while work:
        cur = pop()
        t = type(cur)
        if t is _Lit:
            app(cur)
            continue
        if t is _Exit:
            onpath.discard(cur.key)
            continue
        nodes += 1
        if nodes > MAX_NODES:
            raise Accepted
        if cur is None:
            app("null")
        elif t is str:
            app(_dumps(cur))
        elif t is bool:
            app("true" if cur else "false")
        elif t is int:
            app(int.__repr__(cur))
        elif t is float:
            app(_float_text(cur))
        else:
            if hook is not None:
                rep = hook(cur)
                if rep is not None:
                    push(rep)
                    nodes -= 1
                    continue
            _compound(cur, t, app, push, onpath)
    return "".join(out)


def _compound(cur, t, app, push, onpath) -> None:
    if isinstance(cur, str):
        app(_dumps(cur))
    elif isinstance(cur, bool):
        app("true" if cur else "false")
    elif isinstance(cur, int):
        app(int.__repr__(cur))
    elif isinstance(cur, float):
        app(_float_text(cur))
    elif isinstance(cur, Tagged):
        push(_Lit("}}"))
        push(cur.value)
        push(_Lit('{"$tagged":{"tag":%s,"value":' % _dumps(cur.tag)))
        # pushed in reverse: the opener is read first
    elif isinstance(cur, (bytes, bytearray)):
        # The kit has no byte type; a library that decodes a binary scalar into bytes has not kept
        # the text, so the bytes are written as the string they spell.
        app(_dumps(bytes(cur).decode("utf-8", errors="replace")))
    elif isinstance(cur, datetime.datetime | datetime.date | datetime.time):
        # The core schema has no timestamp: a library that resolves one has changed the value, and
        # the text it prints will not equal the plain string.
        app(_dumps(cur.isoformat()))
    elif isinstance(cur, dict):
        _map(cur, push, onpath)
    elif isinstance(cur, (list, tuple)):
        key = id(cur)
        if key in onpath:
            raise Failure("other", "the value refers to itself", emulated=("error",))
        onpath.add(key)
        push(_Exit(key))
        push(_Lit("]"))
        items = list(cur)
        for i in range(len(items) - 1, -1, -1):
            push(items[i])
            if i:
                push(_COMMA)
        push(_Lit("["))
    elif isinstance(cur, (set, frozenset)):
        # A set is a mapping whose values are all null (YAML 1.2.2 10.3.1 of the 1.1 type).
        _map({k: None for k in cur}, push, onpath, key=id(cur))
    else:
        raise Declined(f"the value has a type with no notation: {t.__module__}.{t.__qualname__}")


def _map(d, push, onpath, key=None) -> None:
    key = id(d) if key is None else key
    if key in onpath:
        raise Failure("other", "the value refers to itself", emulated=("error",))
    onpath.add(key)
    push(_Exit(key))
    items = list(d.items())
    plain = all(type(k) is str and not k.startswith("$") for k, _ in items)
    if plain:
        push(_Lit("}"))
        for i in range(len(items) - 1, -1, -1):
            k, v = items[i]
            push(v)
            push(_Lit(_dumps(k) + ":"))
            if i:
                push(_COMMA)
        push(_Lit("{"))
    else:
        push(_Lit("]}"))
        for i in range(len(items) - 1, -1, -1):
            k, v = items[i]
            push(_Lit("]"))
            push(v)
            push(_COMMA)
            push(k)
            push(_Lit("["))
            if i:
                push(_COMMA)
        push(_Lit('{"$map":['))


def documents_json(docs, hook=None) -> str:
    return to_json(list(docs), hook)


# ---------------------------------------------------------------------------------------------
# Kit values: request JSON -> library values (dump)
# ---------------------------------------------------------------------------------------------

# A mapping is filled after its children exist, so the tree is built first with placeholders and
# the maps are closed afterwards, innermost first, which keeps the whole walk iterative.
def from_kit(root, binary=None, collection_key=None):
    """Turn a kit value (parsed JSON) into plain Python data, with an explicit stack.

    `binary(bytes)` makes the library's value for a byte string; None declines byte strings.
    `collection_key(value)` makes the hashable form of a list or mapping used as a key; None
    declines such a mapping. A mapping Python cannot hold (keys such as 1, 1.0 and true that a
    dict would merge) is declined."""
    kinds: list = []  # (placeholder list, original keys or None) in creation order
    result = [None]
    tasks = [(root, result, 0)]
    while tasks:
        src, dst, idx = tasks.pop()
        if isinstance(src, list):
            out = [None] * len(src)
            for i in range(len(src) - 1, -1, -1):
                tasks.append((src[i], out, i))
            dst[idx] = out
        elif isinstance(src, dict):
            if len(src) == 1:
                (k, v), = src.items()
                if k == "$float":
                    dst[idx] = float(v)
                    continue
                if k == "$tagged":
                    tag = v["tag"]
                    if tag == BINARY_TAG and binary is not None:
                        dst[idx] = binary(base64.b64decode(v["value"]))
                        continue
                    if tag == BINARY_TAG:
                        raise Declined("the library has no byte string")
                    raise Declined(f"the library has no value for the tag {tag}")
                if k == "$map":
                    cells = [[None, None] for _ in v]
                    for i in range(len(v) - 1, -1, -1):
                        tasks.append((v[i][1], cells[i], 1))
                        tasks.append((v[i][0], cells[i], 0))
                    box = ["map", cells]
                    dst[idx] = box
                    kinds.append((dst, idx, box))
                    continue
            keys = list(src.keys())
            cells = [[k, None] for k in keys]
            for i in range(len(keys) - 1, -1, -1):
                tasks.append((src[keys[i]], cells[i], 1))
            box = ["obj", cells]
            dst[idx] = box
            kinds.append((dst, idx, box))
        else:
            dst[idx] = src
    # Close the maps innermost first: a map is created before the maps inside it.
    for dst, idx, box in reversed(kinds):
        d = {}
        for k, val in box[1]:
            if isinstance(k, (list, dict)):
                if collection_key is None:
                    raise Declined("a Python mapping cannot have a collection as a key")
                k = collection_key(k)
            d[k] = val
        if len(d) != len(box[1]):
            raise Declined("a Python mapping merges keys such as 1, 1.0 and true into one")
        # The slot that held the box may be inside a list that was already handed on; replace it.
        dst[idx] = d
    return result[0]
