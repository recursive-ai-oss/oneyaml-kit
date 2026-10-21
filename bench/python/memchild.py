"""The memory run of one cell: a fresh process builds the runner and its starting state, runs the task once
and prints the resident-set growth of that run as JSON (see measure.py)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The harness leaves nothing behind in the checkout, and pyperf starts its workers without the caller's environment.
sys.dont_write_bytecode = True

import measure
from common import load_workload
from registry import build_runner, find

# A 51-byte document every workload set holds. One run on it before the measured run takes the one-time
# costs of a library (static tables, thread pools, lazily imported modules) out of the figure, which
# should be the cost of the document and not of the library's first use.
WARM_UP = "reference-a"


def warm_up(workloads: str, library: str, task: str) -> None:
    d = Path(workloads) / WARM_UP
    if not d.is_dir():
        return
    w = load_workload(d)
    # Typed loading is the generic load followed by a conversion; the tiny workload has no model.
    task = "generic-load" if task == "typed-load" else task
    if task not in w.tasks:
        return
    runner, _ = find(library).runner(task, w)
    if runner is None:
        return
    try:
        runner.run(runner.setup())
    except Exception:  # noqa: BLE001 - only a warm-up: the measured run reports a real failure
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    for k in ("workloads", "library", "task", "workload"):
        ap.add_argument("--" + k, required=True)
    a = ap.parse_args()
    warm_up(a.workloads, a.library, a.task)
    _, runner, _ = build_runner(a.workloads, a.library, a.task, a.workload)
    print(json.dumps({"bytes": measure.memory_of_one_run(runner)}))


if __name__ == "__main__":
    main()
