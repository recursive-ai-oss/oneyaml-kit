"""The yamlrocks adapter.

Load and dump are the library's defaults. Patches use its round-trip mode (OPT_ROUND_TRIP): the
adapter finds the node a JSON Pointer names, and the library's own item assignment and deletion
change the document, which it writes back with its original layout. The library has no insert
into a sequence, so `add` works on mappings only and the adapter declines the rest.
"""

from __future__ import annotations

import re
from importlib import metadata

import yamlrocks

import pykit
from pykit import Declined, Failure, ok

RFC_OPS = ("add", "remove", "replace", "move", "copy", "test")
INDEX = re.compile(r"0|[1-9][0-9]*\Z")


def classify(e):
    if isinstance(e, yamlrocks.YAMLRocksDuplicateKeyError):
        return Failure("duplicate-key", str(e), getattr(e, "line", None), getattr(e, "column", None))
    if isinstance(e, yamlrocks.YAMLRocksParseError):
        return Failure("syntax", str(e), e.line, e.column)
    if isinstance(e, yamlrocks.YAMLRocksError):
        return Failure("other", str(e), getattr(e, "line", None), getattr(e, "column", None))
    return None


# --- pointers ----------------------------------------------------------------------------------

def tokens_of(path):
    if path == "":
        return []
    if not isinstance(path, str) or not path.startswith("/"):
        raise Failure("patch-invalid", "a pointer must be empty or start with a slash",
                      emulated=("error",))
    out = []
    for raw in path[1:].split("/"):
        if re.search(r"~(?![01])", raw):
            raise Failure("patch-invalid", "invalid escape in a pointer", emulated=("error",))
        out.append(raw.replace("~1", "/").replace("~0", "~"))
    return out


def plain_of(node):
    """The plain value of a node (a document, a view, or a scalar the library already unwrapped)."""
    if isinstance(node, yamlrocks.YAMLRocksDocument):
        return node.to_dict()
    if isinstance(node, yamlrocks.YAMLRocksDocumentView):
        return node.unwrap()
    return node


def missing(why):
    return Failure("patch-path", why, emulated=("error",))


def index_of(tok, seq_len, allow_end=False):
    if tok == "-" and allow_end:
        return seq_len
    if not INDEX.match(tok):
        raise missing(f"{tok!r} is not an index")
    i = int(tok)
    if i >= seq_len + (1 if allow_end else 0):
        raise missing("index out of range")
    return i


def child(node, tok):
    plain = plain_of(node)
    if isinstance(plain, dict):
        if tok not in plain:
            raise missing(f"no member {tok!r}")
        return node[tok]
    if isinstance(plain, list):
        return node[index_of(tok, len(plain))]
    raise missing("a scalar has no members")


def apply_op(doc, op):
    if not isinstance(op, dict) or not isinstance(op.get("op"), str):
        raise Failure("patch-invalid", "an operation must be an object with an op", emulated=("error",))
    name = op["op"]
    if name not in RFC_OPS:
        raise Failure("patch-invalid", f"unknown operation {name!r}", emulated=("error",))
    if name not in ("add", "remove", "replace"):
        raise Declined(f"the adapter maps {name} onto nothing in the library")
    if "path" not in op or (name != "remove" and "value" not in op):
        raise Failure("patch-invalid", "the operation lacks a member", emulated=("error",))
    toks = tokens_of(op["path"])
    if not toks:
        raise Declined("the library cannot replace or remove the root of a document")
    parent = doc
    for tok in toks[:-1]:
        parent = child(parent, tok)
    last = toks[-1]
    plain = plain_of(parent)
    if isinstance(plain, dict):
        if name != "add" and last not in plain:
            raise missing(f"no member {last!r}")
        key = last
    elif isinstance(plain, list):
        key = index_of(last, len(plain), allow_end=(name == "add"))
        if name == "add":
            raise Declined("the library has no way to insert into a sequence")
    else:
        raise missing("a scalar has no members")
    if name == "remove":
        del parent[key]
    else:
        parent[key] = op["value"]


def main() -> None:
    def events(data, args):
        yamlrocks.loads_all(data)
        raise pykit.Accepted

    def value(data, args):
        docs = yamlrocks.loads_all(data)
        return ok(value=pykit.JSONText(pykit.documents_json(docs)))

    def dump(data, args):
        obj = pykit.from_kit(args["value"], binary=bytes)
        try:
            out = yamlrocks.dumps(obj)
        except yamlrocks.YAMLRocksError as e:
            raise Failure("other", str(e)) from None
        return ok(output=pykit.b64(out))

    def patch(data, args):
        if args.get("format") == "merge-patch":
            raise Declined("merge patch")

        def refuse(f):
            f.output = data
            f.emulated = sorted(set(f.emulated) | {"unchanged"})
            raise f

        try:
            docs = yamlrocks.loads_all(data, option=yamlrocks.OPT_ROUND_TRIP)
        except Exception as e:
            f = classify(e)
            if f is None:
                raise
            refuse(f)
        index = args.get("document", 0)
        if not 0 <= index < len(docs):
            refuse(Failure("patch-path", f"the stream has {len(docs)} documents, not {index + 1}",
                           emulated=("error",)))
        ops = args["patch"]
        if not isinstance(ops, list):
            refuse(Failure("patch-invalid", "a patch is an array", emulated=("error",)))
        try:
            for op in ops:
                apply_op(docs[index], op)
        except Failure as f:
            f.emulated = [*f.emulated, "pointer"]
            refuse(f)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            # The library refused an assignment the adapter thought possible.
            refuse(Failure("other", f"{type(e).__name__}: {e}"))
        return ok(output=pykit.b64(b"".join(d.to_yaml() for d in docs)), emulated=["pointer"])

    hello = {
        "name": "yamlrocks", "version": metadata.version("yamlrocks"), "language": "python",
        "capabilities": ["outcome:events", "value", "dump", "error-category", "error-position",
                         "patch:add", "patch:remove", "patch:replace"],
        "emulates": ["pointer", "error", "unchanged"],
        "info": {"python": pykit.python_version(), "patch": "OPT_ROUND_TRIP documents"},
    }
    pykit.serve(hello, {"events": events, "value": value, "dump": dump, "patch": patch}, classify)


main()
