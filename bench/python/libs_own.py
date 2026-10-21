"""oneyaml-python, the family's library, in both of its backends.

Present only when the build installed it from a checkout named by KIT_ONEYAML_PYTHON_SRC (see
build.sh, which records the revision beside the environment). The native backend is what the
installed package picks by default; the pure Python backend is forced with ONEYAML_BACKEND=pure,
which must be set before the package is imported. Each cell runs in a process of its own, so the two
libraries never share an interpreter.

The library has no feature to measure yet, so every task is `unsupported`. When a task exists, give
the library a runner for it here the way libs.py does for a competitor, and take the task out of
UNSUPPORTED.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from common import TASKS, Lib

# Written by build.sh into the environment's root when it installed the library.
RECORD = Path(sys.prefix) / "oneyaml-bench-own.json"

UNSUPPORTED = {t: "the library has no API for this task yet" for t in TASKS}


class OwnLib(Lib):
    role = "own"
    dist = "oneyaml"

    def __init__(self, id: str, backend: str, revision: str):
        self.id, self.backend, self.revision = id, backend, revision
        self.settings = {"api": "oneyaml", "backend": backend,
                         "defaults": "the library's own (limits on)"}

    def runner(self, task, w):
        # The backend is chosen when the package is first imported, so it is set before that and the
        # import is the proof that the install works.
        if self.backend == "pure":
            os.environ["ONEYAML_BACKEND"] = "pure"
        else:
            os.environ.pop("ONEYAML_BACKEND", None)
        import oneyaml  # noqa: F401

        return None, UNSUPPORTED[task]


def own_libraries() -> list:
    if not RECORD.is_file():
        return []
    rev = json.loads(RECORD.read_text(encoding="utf-8"))["revision"]
    return [OwnLib("oneyaml-python", "native", rev), OwnLib("oneyaml-python-pure", "pure", rev)]
