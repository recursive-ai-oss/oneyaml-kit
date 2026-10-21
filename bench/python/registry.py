"""Which libraries the harness has, and how a cell finds its runner."""

from __future__ import annotations

from pathlib import Path

from common import load_workload
from controls import controls, selftest_enabled
from libs import competitors, version_of
from libs_own import own_libraries


def all_libraries() -> list:
    libs = [*own_libraries(), *competitors()]
    if selftest_enabled():
        libs += controls()
    return libs


def version(lib) -> str:
    return getattr(lib, "revision", None) or (lib.version() if hasattr(lib, "version") else version_of(lib.dist))


def find(library_id: str):
    for lib in all_libraries():
        if lib.id == library_id:
            return lib
    raise SystemExit(f"no library {library_id!r}")


def build_runner(workloads: str, library: str, task: str, workload: str):
    """(workload, runner, reason): the one place a cell's runner is made, for the check, the clock and
    the memory run alike, so the code judged is the code timed."""
    w = load_workload(Path(workloads) / workload)
    runner, why = find(library).runner(task, w)
    if runner is not None:
        runner.input_bytes = len(w.input)
    return w, runner, why
