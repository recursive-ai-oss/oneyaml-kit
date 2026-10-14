"""JSON loading, value checking, hashing and comparison that never recurse.

A response, an expected value or an upstream file can be nested a hundred thousand levels
deep. The standard library parser and Python's own recursive operations (comparison, repr,
copy, dumps) give up long before that, and an exception there would end a whole run for
one hostile case. Everything here walks with an explicit stack, so depth costs memory and
nothing else.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from json.decoder import scanstring
from typing import Any

ESCAPES = ("$float", "$map", "$tagged")

_WS = re.compile(r"[ \t\r\n]*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?")
_LITERALS = {"true": True, "false": False, "null": None}
_NO_KEY = object()


def _no_duplicates(pairs: list) -> dict:
    """An object with a repeated key has no single meaning, so it is refused, not collapsed."""
    d: dict = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"duplicate key {k!r} in a JSON object")
        d[k] = v
    return d


_DECODER = json.JSONDecoder(object_pairs_hook=_no_duplicates)


def loads(data: bytes | str) -> Any:
    """json.loads that refuses duplicate keys, falling back to an iterative parser when the
    input is too deep for the standard one."""
    if isinstance(data, (bytes, bytearray)):
        data = bytes(data).decode("utf-8")
    try:
        return _DECODER.decode(data)
    except RecursionError:
        return _loads_iterative(data)


def raw_decode(text: str, idx: int = 0) -> tuple[Any, int]:
    """One JSON value starting at `idx` and the offset after it (for streams of values)."""
    try:
        return _DECODER.raw_decode(text, idx)
    except RecursionError:
        return _loads_iterative(text, idx, partial=True)


def _loads_iterative(text: str, pos: int = 0, partial: bool = False) -> Any:
    stack: list = []
    keys: list = []  # pending key of each open object, _NO_KEY for lists
    n = len(text)
    state = "value"
    result: Any = None

    def fail(msg: str):
        raise ValueError(f"invalid JSON at offset {pos}: {msg}")

    def attach(v: Any) -> None:
        nonlocal result
        if not stack:
            result = v
        elif keys[-1] is _NO_KEY:
            stack[-1].append(v)
        elif keys[-1] in stack[-1]:
            fail(f"duplicate key {keys[-1]!r}")
        else:
            stack[-1][keys[-1]] = v

    while True:
        pos = _WS.match(text, pos).end()
        ch = text[pos:pos + 1]
        if state == "done":
            if partial:
                return result, pos
            if pos != n:
                fail("extra data")
            return result
        if state in ("value", "value_or_close"):
            if ch == "[":
                new: Any = []
                attach(new)
                stack.append(new)
                keys.append(_NO_KEY)
                state, pos = "value_or_close", pos + 1
            elif ch == "{":
                new = {}
                attach(new)
                stack.append(new)
                keys.append(None)
                state, pos = "key_or_close", pos + 1
            elif ch == "]" and state == "value_or_close":
                stack.pop()
                keys.pop()
                state, pos = ("after" if stack else "done"), pos + 1
            elif ch == '"':
                s, pos = scanstring(text, pos + 1)
                attach(s)
                state = "after" if stack else "done"
            else:
                m = _NUMBER.match(text, pos)
                if m:
                    attach(float(m.group()) if (m.group(1) or m.group(2)) else int(m.group()))
                    pos = m.end()
                else:
                    word = next((w for w in _LITERALS if text.startswith(w, pos)), None)
                    if word is None:
                        fail("expected a value")
                    attach(_LITERALS[word])
                    pos += len(word)
                state = "after" if stack else "done"
        elif state in ("key", "key_or_close"):
            if ch == "}" and state == "key_or_close":
                stack.pop()
                keys.pop()
                state, pos = ("after" if stack else "done"), pos + 1
            elif ch == '"':
                keys[-1], pos = scanstring(text, pos + 1)
                state = "colon"
            else:
                fail("expected a key")
        elif state == "colon":
            if ch != ":":
                fail("expected ':'")
            state, pos = "value", pos + 1
        else:  # after a value
            if ch == ",":
                state, pos = ("value" if isinstance(stack[-1], list) else "key"), pos + 1
            elif (ch == "]" and isinstance(stack[-1], list)) or (ch == "}" and isinstance(stack[-1], dict)):
                stack.pop()
                keys.pop()
                state, pos = ("after" if stack else "done"), pos + 1
            else:
                fail("expected ',' or a closing bracket")


# --------------------------------------------------------------------------
# Kit values: validation, digests, equality


def _children(v: Any):
    """The kit-value children of a node, in order; raises ValueError for a malformed node."""
    if isinstance(v, list):
        return list(v)
    if isinstance(v, dict):
        dollar = [k for k in v if isinstance(k, str) and k.startswith("$")]
        if not dollar:
            return list(v.values())
        if len(v) != 1 or dollar[0] not in ESCAPES:
            raise ValueError("an object with a $-prefixed key must be exactly one escape form")
        k, x = dollar[0], v[dollar[0]]
        if k == "$float":
            if x not in ("inf", "-inf", "nan"):
                raise ValueError("$float must be inf, -inf or nan")
            return []
        if k == "$tagged":
            if (not isinstance(x, dict) or set(x) != {"tag", "value"} or not isinstance(x["tag"], str)
                    or not x["tag"]):
                raise ValueError('$tagged must be {"tag": "...", "value": ...}')
            return [x["value"]]
        if not isinstance(x, list) or not all(isinstance(p, list) and len(p) == 2 for p in x):
            raise ValueError("$map must be a list of [key, value] pairs")
        return [c for p in x for c in p]
    return []


def check_kit_value(v: Any) -> None:
    """Raise ValueError unless `v` is a kit value (see docs/case-format.md). Iterative."""
    todo = [v]
    while todo:
        x = todo.pop()
        if x is None or isinstance(x, (bool, int, str)):
            continue
        if isinstance(x, float):
            if math.isnan(x) or math.isinf(x):
                raise ValueError('non-finite floats are written {"$float": ...}')
            continue
        if not isinstance(x, (list, dict)):
            raise ValueError(f"not a kit value: {type(x).__name__}")
        todo.extend(_children(x))


def _h(*parts: bytes) -> bytes:
    m = hashlib.sha256()
    for p in parts:
        m.update(len(p).to_bytes(8, "big"))
        m.update(p)
    return m.digest()


def digests(v: Any, ignore_tags: bool = False) -> dict[int, bytes]:
    """A digest for every node of `v`, keyed by id(node): equal digests mean equal values
    (types strict, mappings unordered, `$map` and object forms alike, tags kept unless
    ignored). Raises ValueError if any part is not a kit value. Linear in the size."""
    memo: dict[int, bytes] = {}
    todo: list = [(v, False)]
    while todo:
        x, ready = todo.pop()
        if id(x) in memo and not isinstance(x, (list, dict)):
            continue
        if x is None:
            memo[id(x)] = _h(b"n")
        elif isinstance(x, bool):
            memo[id(x)] = _h(b"b", b"1" if x else b"0")
        elif isinstance(x, int):
            memo[id(x)] = _h(b"i", str(x).encode())
        elif isinstance(x, float):
            if math.isnan(x) or math.isinf(x):
                raise ValueError("non-finite float outside the $float form")
            memo[id(x)] = _h(b"f", repr(x + 0.0).encode())  # -0.0 and 0.0 are one value
        elif isinstance(x, str):
            memo[id(x)] = _h(b"s", x.encode("utf-8", "surrogatepass"))
        elif isinstance(x, (list, dict)):
            if not ready:
                todo.append((x, True))
                todo.extend((c, False) for c in _children(x))
                continue
            memo[id(x)] = _node_digest(x, memo, ignore_tags)
        else:
            raise ValueError(f"not a kit value: {type(x).__name__}")
    return memo


def _node_digest(x: Any, memo: dict, ignore_tags: bool) -> bytes:
    if isinstance(x, list):
        return _h(b"l", *[memo[id(c)] for c in x])
    dollar = [k for k in x if isinstance(k, str) and k.startswith("$")]
    if not dollar:
        pairs = [_h(_h(b"s", k.encode("utf-8", "surrogatepass")), memo[id(c)]) for k, c in x.items()]
    else:
        k, body = dollar[0], x[dollar[0]]
        if k == "$float":
            return _h(b"f", body.encode())
        if k == "$tagged":
            inner = memo[id(body["value"])]
            return inner if ignore_tags else _h(b"t", body["tag"].encode("utf-8", "surrogatepass"), inner)
        pairs = [_h(memo[id(a)], memo[id(b)]) for a, b in body]
    return _h(b"m", *sorted(pairs))


def values_equal(a: Any, b: Any, ignore_tags: bool = False) -> bool:
    try:
        return digests(a, ignore_tags)[id(a)] == digests(b, ignore_tags)[id(b)]
    except (ValueError, KeyError, TypeError):
        return False


def _unwrap(v: Any, ignore_tags: bool) -> Any:
    while (ignore_tags and isinstance(v, dict) and set(v) == {"$tagged"}
           and isinstance(v["$tagged"], dict) and "value" in v["$tagged"]):
        v = v["$tagged"]["value"]
    return v


def short(x: Any, n: int = 160) -> str:
    """repr limited in length, and safe for values too deep to print."""
    try:
        s = repr(x)
    except RecursionError:
        return "<value nested too deeply to print>"
    return s if len(s) <= n else s[: n - 3] + "..."


def _path_text(parts: list[str], limit: int = 120) -> str:
    p = "$" + "".join(parts)
    if len(p) <= limit:
        return p
    return "$..." + p[-(limit - 4):]


def value_diff(exp: Any, act: Any, ignore_tags: bool = False) -> str:
    """Where two kit values first differ, as a path, for the failure detail. '' if equal."""
    try:
        de, da = digests(exp, ignore_tags), digests(act, ignore_tags)
    except (ValueError, KeyError, TypeError):
        return "$: not a kit value"
    if de[id(exp)] == da[id(act)]:
        return ""
    path: list[str] = []
    e, a = _unwrap(exp, ignore_tags), _unwrap(act, ignore_tags)
    while True:
        if isinstance(e, list) and isinstance(a, list):
            nxt = next(((i, x, y) for i, (x, y) in enumerate(zip(e, a)) if de[id(x)] != da[id(y)]), None)
            if nxt is None:
                return f"{_path_text(path)}: expected {len(e)} items, got {len(a)}"
            i, e2, a2 = nxt
            path.append(f"[{i}]")
            e, a = _unwrap(e2, ignore_tags), _unwrap(a2, ignore_tags)
            continue
        if (isinstance(e, dict) and isinstance(a, dict)
                and not any(isinstance(k, str) and k.startswith("$") for k in list(e) + list(a))):
            for k in e:
                if k not in a:
                    return f"{_path_text(path)}: key {k!r} missing"
                if de[id(e[k])] != da[id(a[k])]:
                    path.append(f".{k}")
                    e, a = _unwrap(e[k], ignore_tags), _unwrap(a[k], ignore_tags)
                    break
            else:
                extra = [k for k in a if k not in e]
                return f"{_path_text(path)}: unexpected key {extra[0]!r}" if extra else \
                    f"{_path_text(path)}: differs"
            continue
        return f"{_path_text(path)}: expected {short(e, 60)}, got {short(a, 60)}"
