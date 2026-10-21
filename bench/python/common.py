"""The contract between the harness and a library, and the common form the guard compares.

Tasks, workloads and what a library supplies are described in docs/benchmark-adapters.md.

The common form of a value has exactly these Python types: None, bool, str, int, float, list and
dict (with str keys). Types are compared exactly, so the integer 1 is not the float 1.0 and the text
"1" is not the integer 1, and True is not 1 (Python's own `==` would say it is).
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import math
from pathlib import Path

TASKS = ("typed-load", "generic-load", "dump", "document-parse", "unchanged-emit", "edit-script", "patch")


class CompareError(Exception):
    """A value the converter does not know how to put into the common form."""


# ---- the common form ----------------------------------------------------------------------

def key_string(k) -> str:
    """The text of a mapping key that is not a string. JSON, the form the expectations are written in,
    has only string keys."""
    if isinstance(k, str):
        return str(k)
    if k is None:
        return "null"
    if isinstance(k, bool):
        return "true" if k else "false"
    if isinstance(k, int):
        return str(int(k))
    if isinstance(k, float):
        return repr(float(k))
    raise CompareError(f"mapping key of type {type(k).__name__}")


def to_common(v):
    """What a library produced, in the common form. A type the converter does not know is an error,
    never a guess; a timestamp the library resolved is written as a marked text, so it differs from
    the plain text the specification gives."""
    if v is None:
        return None
    if isinstance(v, bool):
        return bool(v)
    if isinstance(v, int):
        # ruamel.yaml keeps an anchored boolean as an int subclass of its own.
        if type(v).__name__ == "ScalarBoolean":
            return bool(v)
        return int(v)
    if isinstance(v, float):
        return float(v)
    if isinstance(v, str):
        return str.__str__(v)
    if isinstance(v, dict):
        return {key_string(k): to_common(e) for k, e in v.items()}
    if isinstance(v, (list, tuple)):
        return [to_common(e) for e in v]
    if isinstance(v, datetime.datetime):
        return "timestamp " + v.isoformat()
    if isinstance(v, datetime.date):
        return "timestamp " + v.isoformat()
    raise CompareError(f"value of type {type(v).__module__}.{type(v).__name__}")


def from_dataclass(v):
    """A typed result in the common form, by the field's YAML key. A field holding MISSING was absent in the
    document and is left out, the way an omitted-when-empty field would be."""
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {f.metadata.get("key", f.name): from_dataclass(getattr(v, f.name)) for f in dataclasses.fields(v)
                if getattr(v, f.name) is not MISSING}
    if isinstance(v, dict):
        return {key_string(k): from_dataclass(e) for k, e in v.items()}
    if isinstance(v, (list, tuple)):
        return [from_dataclass(e) for e in v]
    return to_common(v)


class _Missing:
    def __repr__(self):
        return "MISSING"


# The value of a model field that the document did not have.
MISSING = _Missing()


def docs_value(docs: list):
    """The common form of a list of documents: the value itself for one document, a list for several."""
    return docs[0] if len(docs) == 1 else list(docs)


def _shown(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v) + " (float)"
    if v is None:
        return "null"
    if isinstance(v, list):
        return f"a sequence of {len(v)}"
    if isinstance(v, dict):
        return f"a mapping of {len(v)}"
    return repr(v)


def clip(s: str, n: int = 80) -> str:
    return s if len(s) <= n else s[:n] + "…"


def _pointer_escape(k: str) -> str:
    return k.replace("~", "~0").replace("/", "~1")


def _diff_at(at: str, got, want) -> str:
    at = at or "/"
    if len(at) > 48:
        at = "…" + at[-47:]
    return f"at {at}: got {clip(_shown(got))}, expected {clip(_shown(want))}"


def first_diff(got, want, at: str = "") -> str:
    """Where two common-form values first differ, as a pointer and the two sides; "" if equal.
    Iterative, so a deep value cannot exhaust the interpreter's stack."""
    stack = [(got, want, at)]
    while stack:
        g, w, p = stack.pop()
        if type(g) is not type(w):
            return _diff_at(p, g, w)
        if isinstance(w, dict):
            for k in sorted(w):
                if k not in g:
                    return "missing " + p + "/" + _pointer_escape(k)
            extra = sorted(set(g) - set(w))
            if extra:
                return "unexpected " + p + "/" + _pointer_escape(extra[0])
            # Pushed in reverse so the first key (in sorted order) is looked at first.
            for k in sorted(w, reverse=True):
                stack.append((g[k], w[k], p + "/" + _pointer_escape(k)))
        elif isinstance(w, list):
            if len(g) != len(w):
                # A length difference is only reported when no element before it differs.
                for i in range(min(len(g), len(w))):
                    d = first_diff(g[i], w[i], f"{p}/{i}")
                    if d:
                        return d
                return f"{p}: {len(g)} items, expected {len(w)}"
            for i in range(len(w) - 1, -1, -1):
                stack.append((g[i], w[i], f"{p}/{i}"))
        elif isinstance(w, float):
            if not (g == w or (math.isnan(g) and math.isnan(w))):
                return _diff_at(p, g, w)
        elif g != w:
            return _diff_at(p, g, w)
    return ""


def bytes_diff(got: bytes, want: bytes) -> str:
    """Where two byte strings first differ; "" if they are equal."""
    if got == want:
        return ""
    at = 0
    n = min(len(got), len(want))
    while at < n and got[at] == want[at]:
        at += 1

    def ctx(b: bytes) -> str:
        lo = max(0, at - 12)
        return clip(repr(b[lo:at + 24].decode("utf-8", "replace")))

    return (f"byte {at}: got {len(got)} bytes {ctx(got)}, expected {len(want)} bytes {ctx(want)}; "
            + explain_bytes(got.decode("utf-8", "replace"), want.decode("utf-8", "replace")))


def _norm_blank(s: str) -> str:
    return "\n".join(x for x in s.split("\n") if x.strip())


def _norm_trailing(s: str) -> str:
    return "\n".join(x.rstrip(" \t") for x in s.split("\n"))


def _norm_indent(s: str) -> str:
    return "\n".join(x.lstrip(" \t") for x in s.split("\n"))


def _norm_spacing(s: str) -> str:
    out = []
    for x in s.split("\n"):
        t = x.lstrip(" \t")
        out.append(x[:len(x) - len(t)] + " ".join(t.replace("\t", " ").split(" ")))
    return "\n".join(out)


def _norm_quotes(s: str) -> str:
    return s.replace("'", "").replace('"', "")


def _norm_breaks(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


NORMALISERS = (("blank lines", _norm_blank), ("trailing spaces", _norm_trailing), ("indentation", _norm_indent),
               ("spacing inside lines", _norm_spacing), ("quoting", _norm_quotes), ("line breaks", _norm_breaks))


def explain_bytes(got: str, want: str) -> str:
    """What two different texts differ in: the smallest set of the kinds above that makes them equal,
    or else the first line that differs."""
    best = None
    for mask in range(1, 1 << len(NORMALISERS)):
        a, b = got, want
        for i, (_, fn) in enumerate(NORMALISERS):
            if mask & (1 << i):
                a, b = fn(a), fn(b)
        if a == b and (best is None or mask.bit_count() < best.bit_count()):
            best = mask
    if best is not None:
        names = [n for i, (n, _) in enumerate(NORMALISERS) if best & (1 << i)]
        return "the texts differ only in " + " and ".join(names)
    gl, wl = got.split("\n"), want.split("\n")
    for i in range(min(len(gl), len(wl))):
        if gl[i] != wl[i]:
            return f"line {i + 1}: got {clip(json.dumps(gl[i], ensure_ascii=False), 62)}, expected {clip(json.dumps(wl[i], ensure_ascii=False), 62)}"
    return f"got {len(gl)} lines, expected {len(wl)}"


# ---- workloads ----------------------------------------------------------------------------

@dataclasses.dataclass
class Op:
    op: str
    path: str
    value: object = None
    has_value: bool = False


@dataclasses.dataclass
class Seg:
    key: str = ""
    index: int = 0
    is_idx: bool = False


@dataclasses.dataclass
class EditStep:
    op: str
    segs: list
    value: object
    path: str


@dataclasses.dataclass
class Workload:
    id: str
    input: bytes
    documents: int
    tasks: list
    typed: str
    expected: list            # [(variant, value)], "spec" first
    edit_ops: list
    edit_expected: list       # [(variant, bytes)]
    patch_ops: list
    patch_failing: list
    patch_expected: list


def _read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _ops(p: Path, key: str) -> list:
    if not p.exists():
        return []
    return [Op(o["op"], o["path"], o.get("value"), "value" in o) for o in _read_json(p).get(key, [])]


def workload_ids(directory: Path, only=()) -> list:
    idx = _read_json(directory / "index.json")
    return [w["id"] for w in idx["workloads"] if not only or w["id"] in only]


def load_workload(directory: Path) -> Workload:
    meta = _read_json(directory / "workload.json")
    expected = [("spec", _read_json(directory / "expected.json"))]
    if (directory / "expected.merged.json").exists():
        expected.append(("merged", _read_json(directory / "expected.merged.json")))

    def maybe(name):
        p = directory / name
        return [("spec", p.read_bytes())] if p.exists() else []

    return Workload(
        id=meta["id"], input=(directory / "input.yaml").read_bytes(), documents=int(meta.get("documents", 1)),
        tasks=[t for t in meta.get("tasks", []) if t in TASKS], typed=meta.get("typed_model") or "",
        expected=expected, edit_ops=_ops(directory / "edit.json", "ops"),
        edit_expected=maybe("edit.expected.yaml"), patch_ops=_ops(directory / "patch.json", "ops"),
        patch_failing=_ops(directory / "patch.json", "failing_ops"), patch_expected=maybe("patch.expected.yaml"))


def pointer_tokens(ptr: str) -> list:
    if ptr == "":
        return []
    return [t.replace("~1", "/").replace("~0", "~") for t in ptr[1:].split("/")]


def pointer_segments(base, ptr: str) -> list:
    """A pointer resolved against the workload's value: a token is an index where the value there is a
    sequence and a key where it is a mapping."""
    cur, out = base, []
    for t in pointer_tokens(ptr):
        if isinstance(cur, list):
            if not t.isdigit() or int(t) >= len(cur):
                raise ValueError(f"no element {t!r} in {ptr}")
            out.append(Seg(index=int(t), is_idx=True))
            cur = cur[int(t)]
        elif isinstance(cur, dict):
            if t not in cur:
                raise ValueError(f"no key {t!r} in {ptr}")
            out.append(Seg(key=t))
            cur = cur[t]
        else:
            raise ValueError(f"cannot descend into a scalar at {t!r} in {ptr}")
    return out


def resolve_steps(w: Workload, ops: list) -> list:
    base = w.expected[0][1]
    return [EditStep(o.op, pointer_segments(base, o.path), o.value, o.path) for o in ops]


# ---- products and runners ------------------------------------------------------------------

VALUE, BYTES, DOCUMENT = "value", "bytes", "document"


@dataclasses.dataclass
class Product:
    """What a runner's answer becomes for the guard."""
    kind: str
    value: object = None
    has_value: bool = False
    data: bytes = b""
    value_only: bool = False


@dataclasses.dataclass
class FailedPatch:
    errored: bool
    data: bytes


class State:
    """One starting state: the input as this state owns it, whatever the library keeps beside it."""
    __slots__ = ("input", "ctx", "doc")

    def __init__(self, input, ctx=None, doc=None):
        self.input, self.ctx, self.doc = input, ctx, doc


class Runner:
    """One library's way to do one task on one workload.

    setup() builds a fresh starting state (not timed); run(state) is the timed task and borrows the
    state; product(out) converts the output for the guard (not timed). The same run() is judged and
    timed. failing() applies the failing patch (patch runners only).
    """

    def __init__(self, setup, run, product, failing=None):
        self.setup, self.run, self.product, self.failing = setup, run, product, failing

    def check(self) -> Product:
        st = self.setup()
        return self.product(self.run(st))


def copy_bytes(b: bytes) -> bytes:
    # bytes(b) and b[:] return the same object; this makes a new one.
    return memoryview(b).tobytes()


def copy_text(b: bytes) -> str:
    return b.decode("utf-8")


class Lib:
    """A library under test. Subclasses (or the table in libs.py) supply what the library has."""

    id = ""
    dist = ""
    role = "competitor"
    settings: dict = {}

    def runner(self, task: str, w: Workload):
        """(Runner, "") or (None, the reason the library cannot do the task)."""
        raise NotImplementedError

    def tasks(self):
        """The tasks the library takes part in; any other gets no cell at all."""
        return TASKS
