"""Case model and loaders.

A case is language-neutral data: input bytes, one operation and one expectation.
Two sources feed the same in-memory model:

* our own suite (`suites/own/cases/<category>/<name>/case.json` plus raw files);
* the vendored yaml-test-suite release (its own directory layout), with our
  corrected expectations from `corrections.json` kept beside the upstream ones.

Input and expected bytes are only ever read and written as bytes. Nothing in this
module decodes them except where a file is defined to be text (event lists).
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import kitjson

CASE_FORMAT = "oneyaml-kit-case/2"
CORRECTIONS_FORMAT = "oneyaml-kit-corrections/2"

CATEGORIES = ("parse", "schema", "edit", "diagnostics", "safety", "compat", "roundtrip", "emit")
ORIGINS = ("spec", "bug", "fuzz", "real-world", "competitor", "upstream")
OPERATIONS = ("events", "value", "patch", "dump")
EXPECT_KINDS = ("events", "value", "bytes", "error", "bounded", "readback")
# Error categories a case may name. "any" means "an error of any kind".
ERROR_CATEGORIES = (
    "any",
    "syntax",
    "encoding",
    "limit",
    "patch-path",
    "patch-test",
    "patch-invalid",
    "refused-alias",
    "refused-ambiguous",
    "unresolved-tag",
    "duplicate-key",
)
PATCH_OPS = ("add", "remove", "replace", "move", "copy", "test")
PATCH_FORMATS = ("json-patch", "merge-patch")
DEFAULT_PROFILE = "core"

SUITE_YTS = "yaml-test-suite"
SUITE_OWN = "own"

ROOT = Path(__file__).resolve().parents[2]
SUITES = ROOT / "suites"
YTS_RELEASE = "data-2022-01-17"
YTS_DIR = SUITES / "yaml-test-suite" / YTS_RELEASE
YTS_CORRECTIONS = SUITES / "yaml-test-suite" / "corrections.json"
OWN_DIR = SUITES / "own" / "cases"


class CaseError(Exception):
    """A case or suite file is malformed."""


@dataclass
class Case:
    id: str
    suite: str
    category: str
    title: str
    origin: str
    op: str
    input: bytes
    expect: dict
    args: dict = field(default_factory=dict)
    productions: list = field(default_factory=list)
    refs: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    # Our corrected expectation beside the upstream one: {"expect", "reason", "citation"}.
    correction: dict | None = None
    # Where the input came from: optional source, revision, licence, reference strings.
    provenance: dict = field(default_factory=dict)

    @property
    def profile(self) -> str:
        return self.args.get("profile", DEFAULT_PROFILE)


# --------------------------------------------------------------------------
# Expectation parsing


def split_lines(text: str) -> list[str]:
    """Split an event list on newlines only (str.splitlines also splits on U+2028 etc.)."""
    if text.endswith("\n"):
        text = text[:-1]
    if text == "":
        return []
    return text.split("\n")


ESCAPES = kitjson.ESCAPES


def check_kit_value(v: Any, where: str) -> None:
    """Reject anything that is not a kit value (see docs/case-format.md).

    Object keys starting with `$` are reserved for the three escape forms, so a YAML
    mapping that has such a key can only be written in the `$map` form and can never
    be mistaken for an escape. Walks with an explicit stack: depth is not limited.
    """
    try:
        kitjson.check_kit_value(v)
    except ValueError as e:
        raise CaseError(f"{where}: {e}; write other mappings as {{\"$map\": [[key, value], ...]}}") from e


def _positive(d: dict, key: str, where: str) -> float:
    v = d.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise CaseError(f"{where}: {key} must be a positive number")
    return v


def _expect_from_json(d: Any, base: Path | None, where: str, nested: bool = False) -> dict:
    if not isinstance(d, dict):
        raise CaseError(f"{where}: expect must be an object")
    kind = d.get("kind")
    if kind not in EXPECT_KINDS or (nested and kind not in ("events", "value", "bytes")):
        raise CaseError(f"{where}: unknown expect kind {kind!r}")

    def only(*keys: str) -> None:
        extra = set(d) - {"kind", *keys}
        if extra:
            raise CaseError(f"{where}: unknown expect keys {sorted(extra)}")

    def read(name: str) -> bytes:
        if base is None:
            raise CaseError(f"{where}: file reference without a case directory")
        if not isinstance(name, str):
            raise CaseError(f"{where}: file names are strings")
        p = base / name
        if not p.is_file() or p.resolve().parent != base.resolve():
            raise CaseError(f"{where}: missing or out-of-directory file {name!r}")
        return p.read_bytes()

    if kind == "events":
        only("file", "recipe")
        if ("file" in d) == ("recipe" in d):
            raise CaseError(f"{where}: events expectation needs exactly one of file/recipe")
        raw = read(d["file"]) if "file" in d else generate_input(d["recipe"], where, "recipe")
        return {"kind": "events", "events": split_lines(raw.decode("utf-8"))}
    if kind == "bytes":
        only("file", "recipe")
        if ("file" in d) == ("recipe" in d):
            raise CaseError(f"{where}: bytes expectation needs exactly one of file/recipe")
        raw = read(d["file"]) if "file" in d else generate_input(d["recipe"], where, "recipe")
        return {"kind": "bytes", "bytes": raw}
    if kind == "value":
        only("file", "value")
        if ("file" in d) == ("value" in d):
            raise CaseError(f"{where}: value expectation needs exactly one of file/value")
        v = kitjson.loads(read(d["file"])) if "file" in d else d["value"]
        if not isinstance(v, list):
            raise CaseError(f"{where}: value must be a list of documents")
        check_kit_value(v, where)
        return {"kind": "value", "value": v, "tags": "keep"}
    if kind == "readback":
        only()
        return {"kind": "readback"}
    if kind == "bounded":
        only("time_seconds", "memory_mb", "accept", "error", "unchanged")
        ts, mb = _positive(d, "time_seconds", where), _positive(d, "memory_mb", where)
        acc = _expect_from_json(d["accept"], base, where, nested=True) if "accept" in d else None
        err = d.get("error")
        if isinstance(err, str):
            err = [err]
        if err is not None:
            ok = (isinstance(err, list) and err and len(set(err)) == len(err)
                  and all(isinstance(c, str) and c in ERROR_CATEGORIES and c != "any" for c in err))
            if not ok:
                raise CaseError(f"{where}: bounded error is a category or a list of distinct categories "
                                f"naming kinds of error ('any' is not one)")
        if acc is None and err is None:
            raise CaseError(f"{where}: bounded needs an accepted result, an error category, or both")
        unchanged = d.get("unchanged", False)
        if not isinstance(unchanged, bool):
            raise CaseError(f"{where}: bounded unchanged is true or false")
        if unchanged and err is None:
            raise CaseError(f"{where}: bounded unchanged needs an error category: it judges a refusal")
        return {"kind": "bounded", "time_seconds": ts, "memory_mb": mb, "accept": acc, "category": err,
                "unchanged": unchanged}
    only("category", "line", "column", "events_file", "unchanged")
    cat = d.get("category", "any")
    if cat not in ERROR_CATEGORIES:
        raise CaseError(f"{where}: unknown error category {cat!r}")
    for k in ("line", "column"):
        if k in d and (not isinstance(d[k], int) or isinstance(d[k], bool) or d[k] < 1):
            raise CaseError(f"{where}: {k} must be a positive integer")
    if "column" in d and "line" not in d:
        raise CaseError(f"{where}: column needs line")
    ev = None
    if "events_file" in d:
        ev = split_lines(read(d["events_file"]).decode("utf-8"))
    return {
        "kind": "error",
        "category": cat,
        "line": d.get("line"),
        "column": d.get("column"),
        "events": ev,
        "unchanged": bool(d.get("unchanged", False)),
    }


# A patch with tens of thousands of identical operations would be larger than a stored file may be,
# so it can be written as parts too: {"ops": [operations], "repeat": n}, written one after another.
MAX_PATCH_OPS = 1_000_000


def expand_patch_recipe(recipe, where: str) -> list:
    """The operations a patch_recipe describes. The count is worked out before anything is built."""
    if not isinstance(recipe, list) or not recipe or len(recipe) > MAX_RECIPE_PARTS:
        raise CaseError(f"{where}: patch_recipe must be a list of 1 to {MAX_RECIPE_PARTS} parts")
    total = 0
    for i, part in enumerate(recipe):
        w = f"{where}: patch_recipe part {i}"
        if not isinstance(part, dict) or set(part) - {"ops", "repeat"} or not isinstance(part.get("ops"), list) \
                or not part["ops"]:
            raise CaseError(f"{w} holds a non-empty list `ops` and an optional `repeat`")
        for o in part["ops"]:
            if not isinstance(o, dict) or not isinstance(o.get("op"), str):
                raise CaseError(f"{w}: operations must be objects with a string op")
        total += len(part["ops"]) * _int(part.get("repeat", 1), w, "repeat")
        if total > MAX_PATCH_OPS:
            raise CaseError(f"{where}: patch_recipe builds more than {MAX_PATCH_OPS} operations")
    return [dict(o) for part in recipe for _ in range(part.get("repeat", 1)) for o in part["ops"]]


def _check_operation(op: dict, where: str) -> tuple[str, dict]:
    if not isinstance(op, dict) or op.get("type") not in OPERATIONS:
        raise CaseError(f"{where}: operation.type must be one of {OPERATIONS}")
    typ = op["type"]
    args = {k: v for k, v in op.items() if k != "type"}
    allowed = {"profile"}
    if typ == "dump":
        allowed |= {"value"}
        if "value" not in args:
            raise CaseError(f"{where}: a dump operation needs a value")
        check_kit_value(args["value"], where)
    if typ == "patch":
        allowed |= {"format", "patch", "patch_recipe", "document"}
        fmt = args.get("format", "json-patch")
        if fmt not in PATCH_FORMATS:
            raise CaseError(f"{where}: unknown patch format {fmt!r}")
        if ("patch" in args) == ("patch_recipe" in args):
            raise CaseError(f"{where}: patch operation needs exactly one of patch and patch_recipe")
        if "patch_recipe" in args:
            if fmt != "json-patch":
                raise CaseError(f"{where}: only a json-patch can be written as a patch_recipe")
            args["patch"] = expand_patch_recipe(args.pop("patch_recipe"), where)
        if fmt == "json-patch":
            if not isinstance(args["patch"], list):
                raise CaseError(f"{where}: a json-patch is an array of operations")
            for o in args["patch"]:
                # An op name outside RFC 6902 is allowed: it is how a patch-invalid case is written.
                if not isinstance(o, dict) or not isinstance(o.get("op"), str):
                    raise CaseError(f"{where}: patch entries must be objects with a string op")
    extra = set(args) - allowed
    if extra:
        raise CaseError(f"{where}: unknown operation keys {sorted(extra)}")
    return typ, args


def _check_pairing(op: str, expect: dict, where: str) -> None:
    k = expect["kind"]
    result = {"events": "events", "value": "value", "patch": "bytes", "dump": "readback"}[op]
    if op == "dump" and k != "readback":
        raise CaseError(f"{where}: a dump operation is judged by reading its output back (readback)")
    if k not in (result, "error", "bounded"):
        raise CaseError(f"{where}: a {op} operation cannot expect {k}")
    if k == "bounded" and expect["accept"] is not None and expect["accept"]["kind"] != result:
        raise CaseError(f"{where}: a {op} operation can only accept a {result} result")
    if k == "error" and expect["events"] is not None and op != "events":
        raise CaseError(f"{where}: events_file only applies to events operations")
    if k in ("error", "bounded") and expect["unchanged"] and op != "patch":
        raise CaseError(f"{where}: unchanged only applies to patch operations")


CASE_KEYS = {
    "format", "id", "title", "category", "origin", "productions", "refs",
    "operation", "input", "input_recipe", "expect", "tags", "notes", "provenance",
}
PROVENANCE_KEYS = ("source", "revision", "licence", "reference")


# --------------------------------------------------------------------------
# Generated inputs

# A case whose input is too large to store describes it as a list of parts. A part is data, not
# code: nothing in a recipe is evaluated, and the size is worked out before a byte is built.
MAX_GENERATED_BYTES = 64 * 1024 * 1024
MAX_RECIPE_PARTS = 64
_TEXT_KEYS = {"text", "repeat"}
_NUMBERS_KEYS = {"start", "count", "prefix", "suffix", "width"}
_PRODUCT_KEYS = {"prefix", "suffix", "choices"}


def _int(v, where: str, name: str, lo: int = 0) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise CaseError(f"{where}: {name} must be an integer of at least {lo}")
    return v


def _plan(recipe, where: str, what: str = "input_recipe") -> list:
    """Check a recipe and work out its size before a byte is built; returns the parts to write."""
    if not isinstance(recipe, list) or not recipe or len(recipe) > MAX_RECIPE_PARTS:
        raise CaseError(f"{where}: {what} must be a list of 1 to {MAX_RECIPE_PARTS} parts")
    plan, total = [], 0
    for i, part in enumerate(recipe):
        w = f"{where}: {what} part {i}"
        if not isinstance(part, dict):
            raise CaseError(f"{w} must be an object")
        if "text" in part:
            if set(part) - _TEXT_KEYS or not isinstance(part["text"], str):
                raise CaseError(f"{w}: a text part holds a string `text` and an optional `repeat`")
            text = part["text"].encode("utf-8")
            n = _int(part.get("repeat", 1), w, "repeat")
            total += len(text) * n
            plan.append(("text", text, n))
        elif set(part) == {"numbers"} and isinstance(part["numbers"], dict):
            q = part["numbers"]
            if set(q) - _NUMBERS_KEYS or "count" not in q:
                raise CaseError(f"{w}: numbers holds count and optionally {sorted(_NUMBERS_KEYS - {'count'})}")
            n = _int(q["count"], w, "count")
            start = _int(q.get("start", 0), w, "start")
            width = _int(q.get("width", 0), w, "width")
            pre, suf = q.get("prefix", ""), q.get("suffix", "")
            if not isinstance(pre, str) or not isinstance(suf, str):
                raise CaseError(f"{w}: prefix and suffix are strings")
            pre, suf = pre.encode("utf-8"), suf.encode("utf-8")
            digits = max(width, len(str(start + max(n - 1, 0))))
            total += n * (len(pre) + digits + len(suf))
            plan.append(("numbers", (pre, suf, start, width), n))
        elif set(part) == {"product"} and isinstance(part["product"], dict):
            q = part["product"]
            if set(q) - _PRODUCT_KEYS or "choices" not in q:
                raise CaseError(f"{w}: product holds choices and optionally {sorted(_PRODUCT_KEYS - {'choices'})}")
            pre, suf = q.get("prefix", ""), q.get("suffix", "")
            ch = q["choices"]
            if not isinstance(pre, str) or not isinstance(suf, str):
                raise CaseError(f"{w}: prefix and suffix are strings")
            if (not isinstance(ch, list) or not ch
                    or not all(isinstance(c, list) and c and all(isinstance(x, str) for x in c) for c in ch)):
                raise CaseError(f"{w}: choices is a list of non-empty lists of strings")
            ch = [[x.encode("utf-8") for x in c] for c in ch]
            pre, suf = pre.encode("utf-8"), suf.encode("utf-8")
            n = 1
            for c in ch:
                n *= len(c)
                if n > MAX_GENERATED_BYTES:
                    raise CaseError(f"{where}: the generated input is larger than {MAX_GENERATED_BYTES} bytes")
            # Every choice of a stage appears in n / len(stage) lines.
            total += n * (len(pre) + len(suf)) + sum(n // len(c) * sum(len(x) for x in c) for c in ch)
            plan.append(("product", (pre, suf, ch), n))
        else:
            raise CaseError(f"{w} is a text part, a numbers part or a product part")
        if total > MAX_GENERATED_BYTES:
            raise CaseError(f"{where}: the generated input is larger than {MAX_GENERATED_BYTES} bytes")
    return plan


# Pieces are built about this big, so building a large input never holds a second copy of one
# part, and a digest of the input can be taken without holding the input at all.
_CHUNK_BYTES = 1 << 20
_NUMBERS_PER_CHUNK = 1 << 14


def iter_input(recipe, where: str, what: str = "input_recipe"):
    """The bytes a recipe describes, in pieces of about a megabyte (see generate_input)."""
    for kind, a, n in _plan(recipe, where, what):
        if kind == "text":
            if not a:
                continue
            per = max(1, _CHUNK_BYTES // len(a))
            for _ in range(n // per):
                yield a * per
            if n % per:
                yield a * (n % per)
        elif kind == "product":
            pre, suf, ch = a
            # Driven by the combinations themselves, never by the size of a block: a run of
            # empty lines must not end the part early.
            block, size = [], 0
            for c in itertools.product(*ch):
                line = pre + b"".join(c) + suf
                block.append(line)
                size += len(line)
                if size >= _CHUNK_BYTES or len(block) >= _NUMBERS_PER_CHUNK:
                    yield b"".join(block)
                    block, size = [], 0
            if block:
                yield b"".join(block)
        else:
            pre, suf, start, width = a
            for lo in range(start, start + n, _NUMBERS_PER_CHUNK):
                hi = min(lo + _NUMBERS_PER_CHUNK, start + n)
                yield b"".join(pre + b"%0*d" % (width, k) + suf for k in range(lo, hi))


def generate_input(recipe, where: str, what: str = "input_recipe") -> bytes:
    """The bytes a recipe describes: parts in order, each either
    {"text": s, "repeat": n} (the UTF-8 text n times; n defaults to 1) or
    {"numbers": {"count": n, "start": k, "prefix": p, "suffix": s, "width": w}}, which writes
    p + the number + s for k, k+1, ... (n numbers), the number zero-padded to w digits, or
    {"product": {"prefix": p, "suffix": s, "choices": [[a, b], [c, d], ...]}}, which writes
    p + one string of each list + s for every combination (the last list varies fastest).
    The total size is limited to MAX_GENERATED_BYTES.

    A large result is remembered by its recipe, so a process that loads the cases again (the
    kit's own tests do, many times) builds each large input once and shares the bytes."""
    key = json.dumps(recipe, sort_keys=True) if isinstance(recipe, list) else None
    if key is not None and key in _BUILT:
        return _BUILT[key]
    out = bytearray()
    for piece in iter_input(recipe, where, what):
        out += piece
    data = bytes(out)
    if key is not None and len(data) >= _REMEMBER_FROM:
        _BUILT[key] = data
    return data


_BUILT: dict[str, bytes] = {}
_REMEMBER_FROM = 1024 * 1024


def load_own_case(case_dir: Path, cases_root: Path = OWN_DIR, skip_slow: bool = False) -> Case | None:
    """One own case. With `skip_slow`, a case tagged slow comes back as None before its
    generated input and expectation are built, which is most of the cost of the large ones."""
    rel = case_dir.relative_to(cases_root).as_posix()
    where = f"own:{rel}"
    try:
        d = kitjson.loads((case_dir / "case.json").read_bytes())
    except (OSError, ValueError) as e:
        raise CaseError(f"{where}: cannot read case.json: {e}") from e
    if d.get("format") != CASE_FORMAT:
        raise CaseError(f"{where}: format must be {CASE_FORMAT!r}")
    extra = set(d) - CASE_KEYS
    if extra:
        raise CaseError(f"{where}: unknown keys {sorted(extra)}")
    if d.get("id") != rel:
        raise CaseError(f"{where}: id {d.get('id')!r} must equal the directory path {rel!r}")
    if d.get("category") not in CATEGORIES:
        raise CaseError(f"{where}: category must be one of {CATEGORIES}")
    if rel.split("/")[0] != d["category"]:
        raise CaseError(f"{where}: the directory must sit under its category")
    if d.get("origin") not in ORIGINS:
        raise CaseError(f"{where}: origin must be one of {ORIGINS}")
    if not isinstance(d.get("title"), str) or not d["title"]:
        raise CaseError(f"{where}: title is required")
    for key in ("productions", "refs", "tags"):
        v = d.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise CaseError(f"{where}: {key} must be a list of strings")
    if d["origin"] == "spec" and not d.get("refs"):
        raise CaseError(f"{where}: a spec case cites the specification in refs")
    prov = d.get("provenance", {})
    if (not isinstance(prov, dict) or set(prov) - set(PROVENANCE_KEYS)
            or not all(isinstance(v, str) and v for v in prov.values())):
        raise CaseError(f"{where}: provenance holds non-empty strings under {PROVENANCE_KEYS}")
    need = {"bug": ("reference",), "fuzz": ("reference",), "competitor": ("reference",),
            "real-world": ("source", "licence")}.get(d["origin"], ())
    for k in need:
        if k not in prov:
            raise CaseError(f"{where}: a {d['origin']} case records provenance.{k}")
    if skip_slow and "slow" in d.get("tags", []):
        return None
    op, args = _check_operation(d.get("operation"), where)
    if op == "dump":
        # Nothing is read: the value is in the operation, and the library writes the text.
        if "input" in d or "input_recipe" in d:
            raise CaseError(f"{where}: a dump case has no input")
        data = b""
    elif "input_recipe" in d:
        if "input" in d:
            raise CaseError(f"{where}: a case has an input file or an input_recipe, not both")
        data = generate_input(d["input_recipe"], where)
    else:
        input_name = d.get("input", "input.yaml")
        ip = case_dir / input_name
        if not ip.is_file():
            raise CaseError(f"{where}: missing input file {input_name!r}")
        data = ip.read_bytes()
    expect = _expect_from_json(d.get("expect"), case_dir, where)
    _check_pairing(op, expect, where)
    return Case(
        id=f"own:{rel}", suite=SUITE_OWN, category=d["category"], title=d["title"],
        origin=d["origin"], op=op, input=data, expect=expect, args=args,
        productions=list(d.get("productions", [])), refs=list(d.get("refs", [])),
        tags=list(d.get("tags", [])), provenance=dict(prov),
    )


def load_own(cases_root: Path = OWN_DIR, include_slow: bool = True) -> list[Case]:
    out = []
    if not cases_root.is_dir():
        return out
    for cj in sorted(cases_root.rglob("case.json")):
        c = load_own_case(cj.parent, cases_root, skip_slow=not include_slow)
        if c is not None:
            out.append(c)
    return out


def slow_ids(cases_root: Path = OWN_DIR) -> set[str]:
    """Ids of the own cases tagged slow, read without building any of them. A run that leaves
    them out uses this to tell a deviation entry for a case it skipped from one for a case
    that does not exist."""
    out = set()
    if cases_root.is_dir():
        for cj in sorted(cases_root.rglob("case.json")):
            if "slow" in kitjson.loads(cj.read_bytes()).get("tags", []):
                out.add("own:" + cj.parent.relative_to(cases_root).as_posix())
    return out


# --------------------------------------------------------------------------
# yaml-test-suite


def _decode_json_stream(raw: bytes, where: str) -> list:
    text = raw.decode("utf-8")
    i, docs = 0, []
    while True:
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text):
            return docs
        try:
            v, i = kitjson.raw_decode(text, i)
        except ValueError as e:
            raise CaseError(f"{where}: in.json is not a JSON stream: {e}") from e
        docs.append(v)


def load_corrections(path: Path = YTS_CORRECTIONS) -> dict[str, dict]:
    """Corrected expectations, by case id. An entry corrects either the events before an
    error (`events`) or a value (`value`)."""
    if not path.is_file():
        return {}
    d = kitjson.loads(path.read_bytes())
    if d.get("format") != CORRECTIONS_FORMAT:
        raise CaseError(f"corrections: format must be {CORRECTIONS_FORMAT!r}")
    out: dict[str, dict] = {}
    for e in d["entries"]:
        extra = set(e) - {"case", "events", "value", "reason", "citation"}
        if extra:
            raise CaseError(f"corrections {e.get('case')}: unknown keys {sorted(extra)}")
        for k in ("case", "reason", "citation"):
            if not e.get(k):
                raise CaseError(f"corrections {e.get('case')}: missing {k}")
        if ("events" in e) == ("value" in e):
            raise CaseError(f"corrections {e['case']}: needs exactly one of events/value")
        if "value" in e:
            check_kit_value(e["value"], f"corrections {e['case']}")
        if e["case"] in out:
            raise CaseError(f"corrections: {e['case']} is listed twice")
        out[e["case"]] = e
    return out


def _to_kit_value(v: Any) -> Any:
    """Rewrite JSON objects that have `$`-prefixed keys into the `$map` form (iterative)."""
    done: dict[int, Any] = {}
    todo: list = [(v, False)]
    while todo:
        x, ready = todo.pop()
        if isinstance(x, list):
            if not ready:
                todo.append((x, True))
                todo.extend((c, False) for c in x)
            else:
                done[id(x)] = [done.get(id(c), c) for c in x]
        elif isinstance(x, dict):
            if not ready:
                todo.append((x, True))
                todo.extend((c, False) for c in x.values())
            elif any(k.startswith("$") for k in x):
                done[id(x)] = {"$map": [[k, done.get(id(c), c)] for k, c in x.items()]}
            else:
                done[id(x)] = {k: done.get(id(c), c) for k, c in x.items()}
    return done.get(id(v), v)


def _apply_correction(case: Case, c: dict) -> None:
    e = dict(case.expect)
    if "events" in c:
        if case.expect["kind"] != "error":
            raise CaseError(f"corrections: {case.id} is not an error case; events corrections "
                            "apply to the events before an error")
        e["events"] = list(c["events"])
        same = e["events"] == case.expect["events"]
    else:
        if case.expect["kind"] != "value":
            raise CaseError(f"corrections: {case.id} is not a value case")
        e["value"] = c["value"]
        # Kit equality, not Python's: 450 and 450.0 are different values here.
        same = kitjson.values_equal(e["value"], case.expect["value"])
    if same:
        raise CaseError(f"corrections: {case.id} equals the upstream expectation")
    case.correction = {"expect": e, "reason": c["reason"], "citation": c["citation"]}


def yts_revision() -> str:
    """The upstream commit, read from the source record so it has one home."""
    for line in (SUITES / "yaml-test-suite" / "SOURCE.md").read_text().splitlines():
        if line.startswith("- **Revision:**"):
            return line.split("`")[1]
    raise CaseError("SOURCE.md has no Revision line")


def load_yts(root: Path = YTS_DIR, corrections: dict[str, dict] | None = None) -> list[Case]:
    """Every yaml-test-suite case, as an `events` case and, where in.json exists, a `value` case.

    Upstream value expectations come from `in.json`, which has no tags: those cases compare
    with `tags: ignore`, a named projection, and keep exact number typing."""
    if corrections is None:
        corrections = load_corrections()
    case_dirs = []
    for top in sorted(p for p in root.iterdir() if p.is_dir()):
        if (top / "in.yaml").is_file():
            case_dirs.append((top.name, top))
        else:
            for sub in sorted(p for p in top.iterdir() if p.is_dir()):
                if (sub / "in.yaml").is_file():
                    case_dirs.append((f"{top.name}/{sub.name}", sub))
    cases = []
    for cid, d in case_dirs:
        title = (d / "===").read_bytes().decode("utf-8").strip() if (d / "===").is_file() else cid
        data = (d / "in.yaml").read_bytes()
        gid = f"yts:{cid}"
        events = split_lines((d / "test.event").read_bytes().decode("utf-8"))
        if (d / "error").is_file():
            expect = {"kind": "error", "category": "any", "line": None, "column": None,
                      "events": events, "unchanged": False}
        else:
            expect = {"kind": "events", "events": events}
        cases.append(Case(
            id=gid, suite=SUITE_YTS, category="parse", title=title, origin="upstream",
            op="events", input=data, expect=expect,
            tags=["valid" if expect["kind"] == "events" else "error"],
            provenance={"source": "https://github.com/yaml/yaml-test-suite", "revision": yts_revision(),
                        "licence": "MIT"},
        ))
        if (d / "in.json").is_file() and not (d / "error").is_file():
            v = _to_kit_value(_decode_json_stream((d / "in.json").read_bytes(), gid))
            check_kit_value(v, gid)
            cases.append(Case(
                id=f"{gid}@value", suite=SUITE_YTS, category="parse", title=title,
                origin="upstream", op="value", input=data,
                expect={"kind": "value", "tags": "ignore", "value": v}, tags=["valid"],
                provenance=dict(cases[-1].provenance),
            ))
    by_id = {c.id: c for c in cases}
    unknown = set(corrections) - set(by_id)
    if unknown:
        raise CaseError(f"corrections name unknown cases: {sorted(unknown)}")
    for cid, c in corrections.items():
        _apply_correction(by_id[cid], c)
    return cases


def slow_requested() -> bool:
    """True when the environment asks for the slow cases too (KIT_SLOW set to anything but 0 or
    empty). The full gate sets it, so the adapter test modules run them without each one
    growing an option."""
    return os.environ.get("KIT_SLOW", "") not in ("", "0")


def load_all(include_slow: bool | None = None) -> list[Case]:
    """Every case. The ones tagged slow are left out, and not even built, unless
    `include_slow` is true or, when it is None, KIT_SLOW asks for them."""
    if include_slow is None:
        include_slow = slow_requested()
    cases = load_yts() + load_own(include_slow=include_slow)
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise CaseError("duplicate case ids")
    if not include_slow:
        cases = [c for c in cases if "slow" not in c.tags]
    return sorted(cases, key=lambda c: c.id)


# --------------------------------------------------------------------------
# Digests (pins)


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(root: Path) -> str:
    """Digest of every file's relative path and bytes below `root`."""
    h = hashlib.sha256()
    for p in sorted(q for q in root.rglob("*") if q.is_file()):
        h.update(p.relative_to(root).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def own_case_digests(cases_root: Path = OWN_DIR) -> dict[str, str]:
    out = {}
    if cases_root.is_dir():
        for cj in sorted(cases_root.rglob("case.json")):
            out[cj.parent.relative_to(cases_root).as_posix()] = tree_digest(cj.parent)
    return out


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def generated_input_digests(cases_root: Path = OWN_DIR) -> dict[str, dict]:
    """The size and digest of every generated input, so a change in what a recipe builds shows
    even when the recipe text does not change. The input is hashed as it is built."""
    out = {}
    if cases_root.is_dir():
        for cj in sorted(cases_root.rglob("case.json")):
            raw = kitjson.loads(cj.read_bytes())
            if "input_recipe" not in raw:
                continue
            rel = cj.parent.relative_to(cases_root).as_posix()
            h, n = hashlib.sha256(), 0
            for piece in iter_input(raw["input_recipe"], f"own:{rel}"):
                h.update(piece)
                n += len(piece)
            out[rel] = {"bytes": n, "sha256": h.hexdigest()}
    return out


def generated_patch_digests(cases_root: Path = OWN_DIR) -> dict[str, dict]:
    """The operation count and digest of every patch that a patch_recipe builds, so a change in
    what a recipe builds shows even when the recipe text does not change."""
    out = {}
    if cases_root.is_dir():
        for cj in sorted(cases_root.rglob("case.json")):
            raw = kitjson.loads(cj.read_bytes())
            recipe = (raw.get("operation") or {}).get("patch_recipe")
            if recipe is None:
                continue
            rel = cj.parent.relative_to(cases_root).as_posix()
            ops = expand_patch_recipe(recipe, f"own:{rel}")
            blob = json.dumps(ops, separators=(",", ":"), sort_keys=True).encode()
            out[rel] = {"ops": len(ops), "sha256": hashlib.sha256(blob).hexdigest()}
    return out


def _result_recipes(expect) -> list:
    """The recipes that an expectation builds its expected result from: its own, or the one of
    the result it accepts (a bounded case)."""
    out = []
    if isinstance(expect, dict):
        if isinstance(expect.get("recipe"), list):
            out.append(expect["recipe"])
        if isinstance(expect.get("accept"), dict):
            out += _result_recipes(expect["accept"])
    return out


def generated_result_digests(cases_root: Path = OWN_DIR) -> dict[str, dict]:
    """The size and digest of every expected result that a recipe builds, as for generated inputs:
    a change in what a recipe builds shows even when the recipe text does not change."""
    out = {}
    if cases_root.is_dir():
        for cj in sorted(cases_root.rglob("case.json")):
            raw = kitjson.loads(cj.read_bytes())
            recipes = _result_recipes(raw.get("expect"))
            if not recipes:
                continue
            rel = cj.parent.relative_to(cases_root).as_posix()
            h, n = hashlib.sha256(), 0
            for recipe in recipes:
                for piece in iter_input(recipe, f"own:{rel}", "recipe"):
                    h.update(piece)
                    n += len(piece)
            out[rel] = {"bytes": n, "sha256": h.hexdigest()}
    return out


def current_pins() -> dict:
    return {
        "yaml-test-suite": {
            "release": YTS_RELEASE,
            "data_digest": tree_digest(YTS_DIR),
            "corrections_digest": file_digest(YTS_CORRECTIONS) if YTS_CORRECTIONS.is_file() else None,
            "cases": 402,
            "error_cases": 94,
        },
        "own": {"cases": own_case_digests(), "generated_inputs": generated_input_digests(),
                "generated_results": generated_result_digests(),
                "generated_patches": generated_patch_digests()},
    }


def corpus_digest(pins: dict | None = None) -> str:
    pins = pins or current_pins()
    return hashlib.sha256(json.dumps(pins, sort_keys=True).encode()).hexdigest()


def kit_version() -> str:
    return (SUITES / "VERSION").read_text().strip() if (SUITES / "VERSION").is_file() else "0"

