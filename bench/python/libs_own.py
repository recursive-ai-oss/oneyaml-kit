"""oneyaml-python, the family's library, in both of its backends.

Present only when the build installed it from a checkout named by KIT_ONEYAML_PYTHON_SRC (see
build.sh, which records the revision beside the environment). The native backend is what the
installed package picks by default; the pure Python backend is forced with ONEYAML_BACKEND=pure,
which must be set before the package is imported. Each cell runs in a process of its own, so the two
libraries never share an interpreter.

Every task has a runner, driven through the public API alone and through the same code as a
competitor's (YamlLib in libs.py); the settings text names the call each task makes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from common import Lib
from libs import DocAPI, PatchAPI, YamlLib, _steps_walk, op_dicts

# Written by build.sh into the environment's root when it installed the library.
RECORD = Path(sys.prefix) / "oneyaml-bench-own.json"


class OwnLib(Lib):
    role = "own"
    dist = "oneyaml"

    def __init__(self, id: str, backend: str, revision: str):
        self.id, self.backend, self.revision = id, backend, revision
        self.settings = {
            "api": "oneyaml.loads, loads_all for streams, dumps; oneyaml.parse for the lossless document "
                   "(bytes(doc) to write it, item assignment and deletion for edits); Document.patch for "
                   "RFC 6902 (RFC 7396 has Document.merge_patch, which the workload's patch cannot express)",
            "backend": backend, "defaults": "the library's own (limits on)", "input": "bytes"}

    def runner(self, task, w):
        # The backend is chosen when the package is first imported, so it is set before that and the
        # import is the proof that the install works.
        if self.backend == "pure":
            os.environ["ONEYAML_BACKEND"] = "pure"
        else:
            os.environ.pop("ONEYAML_BACKEND", None)
        import oneyaml  # noqa: F401

        return self._lib().runner(task, w)

    def _lib(self):
        import oneyaml

        def parse(_, inp, documents):
            return oneyaml.parse(inp)

        def emit(_, doc):
            return bytes(doc)

        def value(doc):
            return [d.to_python() for d in doc.documents]

        def edit(doc, steps):
            # An edit script addresses nodes by key and index, which the document's views take; the
            # stream stands for its first document.
            _steps_walk(doc, steps, lambda c, k, v: c.__setitem__(k, v), lambda c, k: c.__delitem__(k))

        def apply(_, text, ops):
            # The patch is all or nothing, so a failing step leaves the document as it was.
            doc = oneyaml.parse(text)
            doc.patch(ops)
            return bytes(doc)

        return YamlLib(
            self.id, "oneyaml", self.settings, form="bytes",
            load=lambda _, inp: oneyaml.loads(inp), load_all=lambda _, inp: oneyaml.loads_all(inp),
            dump=lambda _, v: oneyaml.dumps(v), doc=DocAPI(parse, emit, value, edit),
            patch=PatchAPI(op_dicts, apply))


def own_libraries() -> list:
    if not RECORD.is_file():
        return []
    rev = json.loads(RECORD.read_text(encoding="utf-8"))["revision"]
    return [OwnLib("oneyaml-python", "native", rev), OwnLib("oneyaml-python-pure", "pure", rev)]
