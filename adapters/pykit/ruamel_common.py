"""The ruamel.yaml adapter, for the safe loader (pure Python) or the round-trip loader.

ruamel-rt also answers patches the way people combine the libraries today: the document is loaded
with the round-trip loader, jsonpatch (RFC 6902) or json-merge-patch (RFC 7396) edits the loaded
objects, and the round-trip dumper writes the stream back.
"""

from __future__ import annotations

import collections.abc
import io
from importlib import metadata

import json_merge_patch
import jsonpatch
import jsonpointer
import ruamel.yaml
from ruamel.yaml import YAML
from ruamel.yaml import comments as ycomments
from ruamel.yaml import error as yerror

import pykit
from pykit import Failure, ok

CORE = "tag:yaml.org,2002:"


def classify(e):
    if isinstance(e, ruamel.yaml.reader.ReaderError):
        # The reader reports an offset into its buffer, not a line and column.
        return Failure("syntax" if e.encoding == "unicode" else "encoding", str(e))
    if isinstance(e, yerror.MarkedYAMLError):
        m = e.problem_mark or e.context_mark
        line, col = (m.line + 1, m.column + 1) if m is not None else (None, None)
        if isinstance(e, ruamel.yaml.constructor.DuplicateKeyError):
            return Failure("duplicate-key", str(e), line, col)
        if isinstance(e, ruamel.yaml.composer.MaxDepthExceededError):
            return Failure("limit", str(e), line, col)
        if isinstance(e, ruamel.yaml.constructor.ConstructorError):
            if "could not determine a constructor for the tag" in (e.problem or ""):
                return Failure("unresolved-tag", str(e), line, col, emulated=("classification",))
            return Failure("other", str(e), line, col)
        return Failure("syntax", str(e), line, col)
    if isinstance(e, yerror.YAMLError):
        return Failure("other", str(e))
    return None


def tag_of(ev):
    t = getattr(ev, "ctag", None)
    return t.value if t is not None else None


def make_hook():
    wrapped: set[int] = set()

    def hook(o):
        t = type(o)
        if t.__name__ == "ScalarBoolean":
            return bool(int(o))
        if t is ycomments.TaggedScalar:
            tag = o.tag.value if o.tag is not None else None
            return o.value if (tag is None or tag.startswith(CORE)) else pykit.Tagged(tag, o.value)
        if isinstance(o, collections.abc.Set) and not isinstance(o, (set, frozenset)):
            return {k: None for k in o}
        if isinstance(o, ycomments.CommentedKeyMap):
            return dict(o.items())  # a mapping used as a key
        if isinstance(o, ycomments.CommentedBase):
            tag = getattr(o, "tag", None)
            tag = tag.value if tag is not None and hasattr(tag, "value") else None
            if tag and not tag.startswith(CORE):
                if id(o) in wrapped:
                    wrapped.discard(id(o))
                    return None
                wrapped.add(id(o))
                return pykit.Tagged(tag, o)
        return None

    return hook


def new_yaml(round_trip: bool):
    return YAML() if round_trip else YAML(typ="safe", pure=True)


# --- patches ---------------------------------------------------------------------------------

def patch_failure(e):
    """The kit category of a jsonpatch / jsonpointer refusal."""
    msg = str(e)
    if isinstance(e, jsonpatch.JsonPatchTestFailed):
        return "patch-test", ()
    if isinstance(e, jsonpatch.InvalidJsonPatch):
        return "patch-invalid", ()
    if isinstance(e, jsonpatch.JsonPatchConflict):
        # One conflict is a malformed request, not a missing path; only its text tells them apart.
        if "own children" in msg:
            return "patch-invalid", ("classification",)
        return "patch-path", ()
    if isinstance(e, jsonpointer.JsonPointerException):
        if msg.startswith("Location must start with /"):
            return "patch-invalid", ("classification",)
        return "patch-path", ("classification",)
    return None


def run(name: str, round_trip: bool) -> None:
    caps = ["events", "error-events", "value", "dump", "error-category", "error-position"]
    emulates = ["classification"]
    if round_trip:
        caps += ["patch:add", "patch:remove", "patch:replace", "patch:move", "patch:copy",
                 "patch:test", "patch:merge"]
        emulates += ["unchanged", "error"]

    def events(data, args):
        lines = []
        try:
            for ev in new_yaml(round_trip).parse(data):
                line = pykit.event_line(ev, tag_of)
                if line is not None:
                    lines.append(line)
        except Exception as e:
            f = classify(e)
            if f is None:
                raise
            f.events = lines
            raise f from None
        return ok(events=lines)

    def value(data, args):
        docs = list(new_yaml(round_trip).load_all(data))
        return ok(value=pykit.JSONText(pykit.documents_json(docs, make_hook())))

    def dump(data, args):
        obj = pykit.from_kit(args["value"], binary=bytes)
        buf = io.StringIO()
        new_yaml(round_trip).dump(obj, buf)
        return ok(output=pykit.b64(buf.getvalue().encode("utf-8")))

    def patch(data, args):
        emu = ["unchanged"]

        def refuse(f):
            f.output = data
            f.emulated = sorted(set(f.emulated) | set(emu))
            raise f

        y = YAML()
        y.preserve_quotes = True  # what someone who edits with ruamel.yaml switches on first
        try:
            docs = list(y.load_all(data))
        except Exception as e:
            f = classify(e)
            if f is None:
                raise
            refuse(f)
        index = args.get("document", 0)
        if not 0 <= index < len(docs):
            emu.append("error")
            refuse(Failure("patch-path", f"the stream has {len(docs)} documents, not {index + 1}"))
        merge = args.get("format") == "merge-patch"
        try:
            if merge:
                docs[index] = json_merge_patch.merge(docs[index], args["patch"])
            else:
                docs[index] = jsonpatch.JsonPatch(args["patch"]).apply(docs[index], in_place=False)
        except Exception as e:
            r = patch_failure(e)
            if r is None:
                raise
            category, extra = r
            emu.extend(extra)
            refuse(Failure(category, str(e)))
        buf = io.StringIO()
        if len(docs) == 1:
            y.dump(docs[0], buf)
        else:
            y.dump_all(docs, buf)
        return ok(output=pykit.b64(buf.getvalue().encode("utf-8")))

    hello = {
        "name": name, "version": ruamel.yaml.__version__, "language": "python",
        "capabilities": caps, "emulates": emulates,
        "info": {"python": pykit.python_version(), "mode": "round-trip" if round_trip else "safe, pure",
                 "jsonpatch": metadata.version("jsonpatch"),
                 "json-merge-patch": metadata.version("json-merge-patch")},
    }
    handlers = {"events": events, "value": value, "dump": dump}
    if round_trip:
        handlers["patch"] = patch
    pykit.serve(hello, handlers, classify)
