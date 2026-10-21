"""The PyYAML adapter, for either loader: the libyaml-backed one (pyyaml-c) or the pure one
(pyyaml-pure). The library's ordinary safe loader and safe dumper, with default settings."""

from __future__ import annotations

import yaml

import pykit
from pykit import Failure, ok


def run(name: str, loader, dumper, with_libyaml: bool) -> None:
    if with_libyaml and not yaml.__with_libyaml__:
        raise SystemExit("PyYAML was installed without libyaml; the C loader is not available")

    def classify(e):
        if isinstance(e, yaml.reader.ReaderError):
            # The reader reports an offset into its buffer, not a line and column.
            cat = "syntax" if e.encoding == "unicode" else "encoding"
            return Failure(cat, str(e))
        if isinstance(e, yaml.MarkedYAMLError):
            m = e.problem_mark or e.context_mark
            line, col = (m.line + 1, m.column + 1) if m is not None else (None, None)
            if isinstance(e, yaml.constructor.ConstructorError):
                if "could not determine a constructor for the tag" in (e.problem or ""):
                    return Failure("unresolved-tag", str(e), line, col, emulated=("classification",))
                return Failure("other", str(e), line, col)
            return Failure("syntax", str(e), line, col)
        if isinstance(e, yaml.YAMLError):
            return Failure("other", str(e))
        return None

    def tag_of(ev):
        return ev.tag

    def events(data, args):
        lines = []
        try:
            for ev in yaml.parse(data, Loader=loader):
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
        docs = list(yaml.load_all(data, Loader=loader))
        return ok(value=pykit.JSONText(pykit.documents_json(docs)))

    def dump(data, args):
        obj = pykit.from_kit(args["value"], binary=bytes)
        text = yaml.dump(obj, Dumper=dumper)
        return ok(output=pykit.b64(text.encode("utf-8")))

    hello = {
        "name": name, "version": yaml.__version__, "language": "python",
        "capabilities": ["events", "error-events", "value", "dump", "error-category", "error-position"],
        "emulates": ["classification"],
        "info": {"python": pykit.python_version(), "libyaml": bool(yaml.__with_libyaml__),
                 "loader": loader.__name__, "dumper": dumper.__name__},
    }
    pykit.serve(hello, {"events": events, "value": value, "dump": dump}, classify)
