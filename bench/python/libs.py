"""The competitor libraries, each driven through the API its documentation shows for the task.

A library fills in only what it really has; what it leaves out is `unsupported`, with a reason. Library
modules are imported inside the functions that use them, so listing the libraries imports none.

Every library gets the input in the form it takes: text where it takes text, bytes only where it
requires bytes. The copy is made in setup, so no two starting states, and not the workload, share a
buffer, whatever a library does with the one it is given.
"""

from __future__ import annotations

import io
from importlib import metadata

import models
from common import (BYTES, DOCUMENT, TASKS, VALUE, FailedPatch, Lib, Product, Runner, State, Workload, copy_bytes,
                    copy_text, docs_value, from_dataclass, resolve_steps, to_common)


def version_of(dist: str) -> str:
    return metadata.version(dist)


def as_bytes(x) -> bytes:
    return x if isinstance(x, bytes) else x.encode("utf-8")


class DocAPI:
    """The library's lossless document."""

    def __init__(self, parse, emit, value=None, edit=None, why_no_edit="no edit API", make=None):
        self.parse = parse          # parse(ctx, input, documents) -> doc
        self.emit = emit            # emit(ctx, doc) -> text or bytes
        self.value = value          # value(doc) -> list of Python values, one per document (for the guard only)
        self.edit = edit            # edit(doc, steps): replace and remove, in the library's own addressing
        self.why_no_edit = why_no_edit


class PatchAPI:
    def __init__(self, prepare, apply, value_only=False):
        self.prepare = prepare      # prepare(ops) -> the library's patch, built before the clock
        self.apply = apply          # apply(ctx, text, patch) -> text (timed: the task is defined on text)
        self.value_only = value_only


def op_dicts(ops):
    out = []
    for o in ops:
        d = {"op": o.op, "path": o.path}
        if o.has_value:
            d["value"] = o.value
        out.append(d)
    return out


class YamlLib(Lib):
    def __init__(self, id, dist, settings, *, form="str", make=None, load=None, load_all=None, dump=None,
                 doc=None, patch=None, only=None, why=None):
        self.id, self.dist, self.settings = id, dist, settings
        self.form = form
        self.make = make or (lambda: None)
        self.load, self.load_all, self.dump, self.doc, self.patch = load, load_all, dump, doc, patch
        self.only = only
        self.why = why or {}

    def tasks(self):
        return self.only or TASKS

    def own(self, w: Workload):
        """Makes this library's copy of the input, in the form it takes."""
        cp = copy_text if self.form == "str" else copy_bytes
        return lambda: cp(w.input)

    def _state(self, w):
        cp = self.own(w)
        return lambda: State(cp(), self.make())

    def _read(self, ctx, inp, documents):
        if documents <= 1 or self.load_all is None:
            return [self.load(ctx, inp)]
        return list(self.load_all(ctx, inp))

    def runner(self, task, w):
        def no(why, fallback):
            return None, self.why.get(task) or why or fallback

        if task in ("typed-load", "generic-load"):
            if self.load is None:
                return no("", "no decoding to Python values")
            if task == "typed-load":
                return Runner(self._state(w), lambda s: models.convert(w.typed, self.load(s.ctx, s.input)),
                              lambda out: Product(VALUE, from_dataclass(out), True)), ""
            return Runner(self._state(w), lambda s: self._read(s.ctx, s.input, w.documents),
                          lambda docs: Product(VALUE, docs_value([to_common(d) for d in docs]), True)), ""
        if task == "dump":
            if self.dump is None:
                return no("", "no encoder")
            if w.documents > 1:
                return None, "a stream of several documents is not dumped by this task"
            cp = self.own(w)

            def setup():
                # The library's own tree, read from the input, outside the clock.
                s = State(None, self.make())
                s.doc = self._read(s.ctx, cp(), 1)[0]
                return s

            return Runner(setup, lambda s: self.dump(s.ctx, s.doc), lambda out: Product(BYTES, data=as_bytes(out))), ""
        if task in ("document-parse", "unchanged-emit", "edit-script"):
            return self._doc_runner(task, w)
        if self.patch is None:
            return no("", "no patch support")
        return self._patch_runner(w)

    def _doc_runner(self, task, w):
        d = self.doc
        if d is None:
            return None, self.why.get(task) or "no lossless document"
        cp = self.own(w)

        def parse_state():
            # Every run works on its own freshly parsed document, parsed outside the clock. A document
            # that several runs shared would be written warm, and a library that keeps its rendering
            # would be timed on the first write for one run and on a cached one for the rest.
            s = State(None, self.make())
            s.doc = d.parse(s.ctx, cp(), w.documents)
            return s

        if task == "document-parse":
            def product(doc):
                s, parsed = doc
                p = Product(DOCUMENT)
                if d.value is not None:
                    p.value = docs_value([to_common(v) for v in d.value(parsed)])
                    p.has_value = True
                p.data = as_bytes(d.emit(s.ctx, parsed))
                return p

            def run(s):
                return s, d.parse(s.ctx, s.input, w.documents)

            return Runner(self._state(w), run, product), ""
        if task == "unchanged-emit":
            return Runner(parse_state, lambda s: d.emit(s.ctx, s.doc), lambda out: Product(BYTES, data=as_bytes(out))), ""
        if d.edit is None:
            return None, d.why_no_edit
        try:
            steps = resolve_steps(w, w.edit_ops)
        except ValueError as e:
            return None, "the workload's edit cannot be resolved: " + str(e)

        def run_edit(s):
            d.edit(s.doc, steps)
            return d.emit(s.ctx, s.doc)

        return Runner(parse_state, run_edit, lambda out: Product(BYTES, data=as_bytes(out))), ""

    def _patch_runner(self, w):
        pa = self.patch
        try:
            good, bad = pa.prepare(w.patch_ops), pa.prepare(w.patch_failing)
        except Exception as e:  # noqa: BLE001 - a patch the library cannot build is its refusal
            return None, "the patch cannot be built: " + str(e)

        def failing():
            # The library takes the text and returns new text, so the document afterwards is the text
            # the caller still holds: its own copy, compared with the original.
            held = copy_bytes(w.input)
            try:
                pa.apply(self.make(), held.decode("utf-8"), bad)
            except Exception:  # noqa: BLE001 - any error is the library refusing the failing step
                return FailedPatch(True, held)
            return FailedPatch(False, held)

        return Runner(self._state(w), lambda s: pa.apply(s.ctx, s.input, good),
                      lambda out: Product(BYTES, data=as_bytes(out), value_only=pa.value_only), failing), ""


# ---- PyYAML ---------------------------------------------------------------------------------

def lib_pyyaml(c: bool) -> YamlLib:
    def loader():
        import yaml
        return yaml.CSafeLoader if c else yaml.SafeLoader

    def dumper():
        import yaml
        return yaml.CSafeDumper if c else yaml.SafeDumper

    def load(ctx, inp):
        import yaml
        return yaml.load(inp, Loader=loader())

    def load_all(ctx, inp):
        import yaml
        return list(yaml.load_all(inp, Loader=loader()))

    def dump(ctx, v):
        import yaml
        # sort_keys=False keeps the document's order, which the default would sort away.
        return yaml.dump(v, Dumper=dumper(), sort_keys=False)

    name = "CSafeLoader and CSafeDumper (libyaml)" if c else "SafeLoader and SafeDumper (pure Python)"
    return YamlLib(
        "pyyaml-c" if c else "pyyaml-pure", "PyYAML",
        {"api": f"yaml.load with {name}, yaml.load_all for streams, yaml.dump",
         "dump": "sort_keys=False; the rest are the library's defaults",
         "schema": "YAML 1.1 (the library's own)", "input": "str"},
        load=load, load_all=load_all, dump=dump,
        why={t: "no document model: it decodes to values and discards comments and layout"
             for t in ("document-parse", "unchanged-emit", "edit-script")} | {"patch": "no patch support"})


# ---- ruamel.yaml ----------------------------------------------------------------------------

def lib_ruamel_safe() -> YamlLib:
    def make():
        from ruamel.yaml import YAML
        return YAML(typ="safe", pure=True)

    def dump(y, v):
        buf = io.StringIO()
        y.dump(v, buf)
        return buf.getvalue()

    return YamlLib(
        "ruamel-safe", "ruamel.yaml",
        {"api": "YAML(typ='safe', pure=True): load, load_all for streams, dump to a StringIO",
         "defaults": "the library's own", "input": "str"},
        make=make, load=lambda y, s: y.load(s), load_all=lambda y, s: list(y.load_all(s)), dump=dump,
        why={t: "the safe loader keeps no document: it decodes to values and discards comments and layout"
             for t in ("document-parse", "unchanged-emit", "edit-script")} | {"patch": "no patch support"})


def _steps_walk(doc, steps, set_item, del_item):
    for st in steps:
        cur = doc
        for s in st.segs[:-1]:
            cur = cur[s.index if s.is_idx else s.key]
        last = st.segs[-1]
        k = last.index if last.is_idx else last.key
        if st.op == "replace":
            set_item(cur, k, st.value)
        elif st.op == "remove":
            del_item(cur, k)
        else:
            raise ValueError(f"unsupported edit {st.op}")


class _Stream(list):
    """The documents of a stream, to tell them from one document that is itself a list."""


def lib_ruamel_rt() -> YamlLib:
    def make():
        from ruamel.yaml import YAML
        return YAML()

    def dump_to_text(y, v, many=False):
        buf = io.StringIO()
        (y.dump_all if many else y.dump)(v, buf)
        return buf.getvalue()

    def parse(y, inp, documents):
        return _Stream(y.load_all(inp)) if documents > 1 else y.load(inp)

    def emit(y, doc):
        return dump_to_text(y, doc, many=isinstance(doc, _Stream))

    def value(doc):
        return list(doc) if isinstance(doc, _Stream) else [doc]

    def edit(doc, steps):
        _steps_walk(doc, steps, lambda c, k, v: c.__setitem__(k, v), lambda c, k: c.__delitem__(k))

    return YamlLib(
        "ruamel-roundtrip", "ruamel.yaml",
        {"api": "YAML() (round-trip): load, load_all, dump, dump_all to a StringIO; edits assign into the "
                "CommentedMap and CommentedSeq",
         "defaults": "the library's own (indent 2, quotes not preserved)", "input": "str"},
        make=make, load=lambda y, s: y.load(s), load_all=lambda y, s: list(y.load_all(s)),
        dump=lambda y, v: dump_to_text(y, v), doc=DocAPI(parse, emit, value, edit),
        why={"patch": "no patch support on its own; see ruamel-jsonpatch"})


# ---- yamlrocks ------------------------------------------------------------------------------

def lib_yamlrocks() -> YamlLib:
    def load(_, inp):
        import yamlrocks
        return yamlrocks.loads(inp)

    def load_all(_, inp):
        import yamlrocks
        return yamlrocks.loads_all(inp)

    def dump(_, v):
        import yamlrocks
        return yamlrocks.dumps(v)

    return YamlLib(
        "yamlrocks", "yamlrocks",
        {"api": "yamlrocks.loads, loads_all for streams, dumps (bytes)",
         "defaults": "the library's own (YAML 1.2; merge keys applied while loading)", "input": "bytes"},
        form="bytes", load=load, load_all=load_all, dump=dump,
        only=("typed-load", "generic-load", "dump"))


class _Docs(list):
    """The documents of a stream."""


def lib_yamlrocks_roundtrip() -> YamlLib:
    def parse(_, inp, documents):
        import yamlrocks
        if documents > 1:
            return _Docs(yamlrocks.loads_all(inp, option=yamlrocks.OPT_ROUND_TRIP))
        return yamlrocks.loads(inp, option=yamlrocks.OPT_ROUND_TRIP)

    def emit(_, doc):
        return b"".join(d.to_yaml() for d in doc) if isinstance(doc, _Docs) else doc.to_yaml()

    def value(doc):
        return [d.to_dict() for d in doc] if isinstance(doc, _Docs) else [doc.to_dict()]

    def edit(doc, steps):
        _steps_walk(doc, steps, lambda c, k, v: c.__setitem__(k, v), lambda c, k: c.__delitem__(k))

    return YamlLib(
        "yamlrocks-roundtrip", "yamlrocks",
        {"api": "yamlrocks.loads(option=OPT_ROUND_TRIP) and loads_all for streams; to_yaml writes the document; "
                "edits assign into the document",
         "defaults": "the library's own", "input": "bytes"},
        form="bytes", doc=DocAPI(parse, emit, value, edit),
        only=("document-parse", "unchanged-emit", "edit-script"))


# ---- the Rust-backed value libraries ----------------------------------------------------------

def lib_yaml_rs() -> YamlLib:
    def load(_, inp):
        import yaml_rs
        return yaml_rs.loads(inp, parse_datetime=False)

    def dump(_, v):
        import yaml_rs
        return yaml_rs.dumps(v)

    return YamlLib(
        "yaml-rs", "yaml-rs",
        {"api": "yaml_rs.loads (a stream gives a list of documents), yaml_rs.dumps",
         "defaults": "the library's own (duplicate keys are an error), except parse_datetime=False: the YAML 1.2 "
                     "value of a timestamp is its text", "input": "str"},
        load=load, load_all=load, dump=dump,
        only=("typed-load", "generic-load", "dump"))


def lib_py_yaml12() -> YamlLib:
    def load(_, inp):
        import yaml12
        return yaml12.parse_yaml(inp)

    def load_all(_, inp):
        import yaml12
        return yaml12.parse_yaml(inp, multi=True)

    def dump(_, v):
        import yaml12
        return yaml12.format_yaml(v)

    return YamlLib(
        "py-yaml12", "py-yaml12",
        {"api": "yaml12.parse_yaml (multi=True for streams), yaml12.format_yaml",
         "defaults": "the library's own (YAML 1.2 core schema)", "input": "str"},
        load=load, load_all=load_all, dump=dump,
        only=("typed-load", "generic-load", "dump"))


def lib_ryaml() -> YamlLib:
    def load(_, inp):
        import ryaml
        return ryaml.loads(inp)

    def load_all(_, inp):
        import ryaml
        return ryaml.loads_all(inp)

    def dump(_, v):
        import ryaml
        return ryaml.dumps(v)

    return YamlLib(
        "ryaml", "ryaml",
        {"api": "ryaml.loads, loads_all for streams, dumps", "defaults": "the library's own", "input": "str"},
        load=load, load_all=load_all, dump=dump,
        only=("typed-load", "generic-load", "dump"))


# ---- yaml-edit ------------------------------------------------------------------------------

def _collection(node):
    m = node.as_mapping()
    return m if m is not None else node.as_sequence()


def lib_yaml_edit() -> YamlLib:
    def parse(_, inp, documents):
        import yaml_edit
        return yaml_edit.YamlFile.parse(inp)

    def emit(_, doc):
        return str(doc)

    def edit(f, steps):
        d = f.document()
        for st in steps:
            cur = _collection(d)
            if cur is None:
                raise ValueError("the document is not a collection")
            for s in st.segs[:-1]:
                node = cur.get(s.index if s.is_idx else s.key)
                if node is None:
                    raise ValueError(f"no {st.path}")
                cur = _collection(node)
                if cur is None:
                    raise ValueError(f"cannot step into a scalar at {st.path}")
            last = st.segs[-1]
            k = last.index if last.is_idx else last.key
            if st.op == "replace":
                cur.set(k, st.value)
            elif st.op == "remove":
                cur.remove(k)
            else:
                raise ValueError(f"unsupported edit {st.op}")

    return YamlLib(
        "yaml-edit", "yaml-edit",
        {"api": "yaml_edit.YamlFile.parse; Mapping.set and remove, Sequence.set and remove; str() writes it",
         "defaults": "the library's own", "input": "str",
         "document": "lossless: it keeps the source text; it has no read-back of the whole value"},
        doc=DocAPI(parse, emit, None, edit),
        only=("document-parse", "unchanged-emit", "edit-script"))


# ---- patch baselines --------------------------------------------------------------------------

def lib_ruamel_jsonpatch() -> YamlLib:
    def make():
        from ruamel.yaml import YAML
        return YAML()

    def prepare(ops):
        import jsonpatch
        return jsonpatch.JsonPatch(op_dicts(ops))

    def apply(y, text, patch):
        doc = y.load(text)
        # in_place=False works on a copy that keeps the comments, so a patch that fails part-way
        # leaves the parsed document as it was.
        out = patch.apply(doc, in_place=False)
        buf = io.StringIO()
        y.dump(out, buf)
        return buf.getvalue()

    return YamlLib(
        "ruamel-jsonpatch", "jsonpatch",
        {"api": "ruamel.yaml YAML().load, jsonpatch.JsonPatch.apply(in_place=False), YAML().dump (RFC 6902)",
         "ruamel.yaml": version_of("ruamel.yaml"),
         "state": "the text",
         "note": "the baseline is a pipeline, so it parses and writes inside the timed work"},
        make=make, patch=PatchAPI(prepare, apply), only=("patch",))


def lib_ruamel_merge_patch() -> YamlLib:
    why = ("RFC 7396 merge patch has no test step, cannot address an array element and never fails on a "
           "mismatch, so it cannot express the workload's RFC 6902 patch")
    return YamlLib(
        "ruamel-json-merge-patch", "json-merge-patch",
        {"api": "ruamel.yaml YAML().load, json_merge_patch.merge, YAML().dump (RFC 7396)",
         "ruamel.yaml": version_of("ruamel.yaml"), "state": "the text"},
        why={"patch": why}, only=("patch",))


def competitors() -> list:
    return [lib_pyyaml(True), lib_pyyaml(False), lib_ruamel_safe(), lib_ruamel_rt(), lib_yamlrocks(),
            lib_yamlrocks_roundtrip(), lib_yaml_rs(), lib_py_yaml12(), lib_ryaml(), lib_yaml_edit(),
            lib_ruamel_jsonpatch(), lib_ruamel_merge_patch()]
