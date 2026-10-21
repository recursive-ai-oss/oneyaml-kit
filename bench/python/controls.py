"""Control libraries for the harness's own tests.

They are deliberately fake. One returns exactly the expectation, one an altered expectation, one
refuses, one ends the process. The tests run them through every task to prove the guard accepts a
right answer, refuses a wrong one and gives a refused cell no numbers. They are listed only when the
environment variable KIT_BENCH_SELFTEST is set, which no benchmark run sets; a record that lists one
says so in the role.
"""

from __future__ import annotations

import copy
import os

from common import BYTES, DOCUMENT, VALUE, FailedPatch, Lib, Product, Runner, State, copy_bytes

CORRECT, WRONG, REJECT, CRASH = "correct", "wrong", "reject", "crash"


def alter(v):
    if isinstance(v, dict):
        return {**v, "altered": True}
    if isinstance(v, list):
        return [*v, "altered"]
    return "altered"


class Control(Lib):
    role = "control"
    dist = "control"

    def __init__(self, id: str, behaviour: str):
        self.id, self.behaviour, self.settings = id, behaviour, {}

    def version(self) -> str:
        return "0"

    def runner(self, task, w):
        b = self.behaviour
        wrong = b == WRONG

        def product():
            if task in ("typed-load", "generic-load"):
                v = copy.deepcopy(w.expected[0][1])
                return Product(VALUE, alter(v) if wrong else v, True)
            if task in ("dump", "unchanged-emit"):
                data = w.input + (b"altered: true\n" if task == "dump" else b" ") if wrong else w.input
                return Product(BYTES, data=data)
            if task == "document-parse":
                return Product(DOCUMENT, copy.deepcopy(w.expected[0][1]), True, w.input + b" " if wrong else w.input)
            if task == "edit-script":
                return Product(BYTES, data=w.edit_expected[0][1] + (b" " if wrong else b""))
            return Product(BYTES, data=w.patch_expected[0][1] + (b" " if wrong else b""))

        p = product()

        def run(_):
            if b == REJECT:
                raise ValueError("control refuses")
            if b == CRASH:
                os._exit(3)
            return p

        def setup():
            return State(copy_bytes(w.input))

        failing = None
        if task == "patch":
            # A wrong control reports success on a patch with a failing step.
            failing = lambda: FailedPatch(not wrong, copy_bytes(w.input))  # noqa: E731
        return Runner(setup, run, lambda out: out, failing), ""


def controls() -> list:
    return [Control(f"control-{b}", b) for b in (CORRECT, WRONG, REJECT, CRASH)]


def selftest_enabled() -> bool:
    return bool(os.environ.get("KIT_BENCH_SELFTEST"))
