"""In-process benchmark harness for Python YAML libraries.

`run` measures every (library, task, workload) cell that the workload set defines and writes one JSON
line per cell. Each cell is a process of its own (cell.py): it judges the library's answer first, then
times it with pyperf, then measures its memory in a fresh process. A cell that kills its process is
recorded as an error and the run goes on. See docs/benchmarks.md for the protocol and the result format.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path

# The harness leaves nothing behind in the checkout, and pyperf starts its workers without the caller's environment.
sys.dont_write_bytecode = True

import measure
from common import load_workload, workload_ids
from controls import selftest_enabled
from registry import all_libraries, version

HERE = Path(__file__).resolve().parent
NAME, VERSION = "oneyaml-bench-python", "0.1.0"


def parse_args(argv):
    ap = argparse.ArgumentParser(prog=NAME)
    ap.add_argument("cmd", choices=("run", "list"))
    ap.add_argument("--workloads", default="workloads")
    ap.add_argument("--out")
    ap.add_argument("--mode", choices=("quick", "full"), default="quick")
    for k in ("library", "task", "workload", "skip"):
        ap.add_argument("--" + k, action="append", default=[])
    return ap.parse_args(argv)


def pyperf_args(mode: str) -> list:
    return measure.QUICK_ARGS if mode == "quick" else []


def timer_text(mode: str) -> str:
    # pyperf's defaults are 20 worker processes of 3 values after 1 warm-up value, loops calibrated so
    # that one value takes at least 100 ms.
    if mode == "quick":
        return ("mean seconds per run from pyperf, each run on a starting state of its own, at 3 worker processes of "
                "2 values after 1 warm-up value and loops calibrated for 50 ms (20 processes of 3 values and "
                "100 ms in full mode)")
    return ("mean seconds per run from pyperf at its defaults (20 worker processes of 3 values after 1 warm-up "
            "value, loops calibrated for 100 ms), each run on a starting state of its own")


MEMORY_TEXT = ("median over three fresh processes of the peak growth of the resident set during one run, with the "
               "allocator set to malloc, after a warm-up run on a tiny document and with the starting state built "
               "beforehand; it counts Python, C and Rust allocations alike, in pages of 4096 bytes, and no allocation "
               "count is available")


def record(a, libs) -> dict:
    return {
        "type": "harness", "name": NAME, "version": VERSION, "language": "python",
        "runtime": f"{platform.python_implementation()} {platform.python_version()}",
        "target": f"{platform.system().lower()}/{platform.machine()}", "mode": a.mode,
        "tool": f"pyperf {metadata.version('pyperf')}",
        # Every library's patch is timed from text to text: the task is defined on text here.
        "text_tasks": ["patch"],
        "timer": timer_text(a.mode), "memory": MEMORY_TEXT,
        "libraries": [{"id": l.id, "package": l.dist, "version": version(l), "role": l.role,
                       "settings": l.settings} for l in libs],
    }


def run_cell(lib, task: str, workload: str, a) -> dict:
    """The cell record: from cell.py, or an error when the process died without writing one."""
    base = {"type": "cell", "library": lib.id, "task": task, "workload": workload}
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "cell.json"
        cmd = [sys.executable, str(HERE / "cell.py"), "--workloads", a.workloads, "--library", lib.id,
               "--task", task, "--workload", workload, "--cell-out", str(out), "--quiet", *pyperf_args(a.mode)]
        if selftest_enabled():
            # pyperf starts its workers with a clean environment; the control libraries are listed by this variable.
            cmd += ["--inherit-environ", "KIT_BENCH_SELFTEST"]
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if r.returncode == 0 and out.is_file():
            return json.loads(out.read_text(encoding="utf-8"))
        # The last line of what the process said, without the paths of this machine: the detail is published.
        tail = re.sub(r"/[^\s:]+", "…", (r.stderr.strip().splitlines() or [""])[-1])
        what = f"the process died (exit status {r.returncode})" if r.returncode != 0 else "the cell wrote no result"
        return {**base, "status": "error", "detail": f"{what}: {tail}"[:200] if tail else what}


def run(a) -> None:
    libs = [l for l in all_libraries() if not a.library or l.id in a.library]
    ids = workload_ids(Path(a.workloads), a.workload)
    with open(a.out, "w", encoding="utf-8") as f:
        def put(rec):
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()

        put(record(a, libs))
        for wid in ids:
            w = load_workload(Path(a.workloads) / wid)
            for task in w.tasks:
                if a.task and task not in a.task:
                    continue
                for lib in libs:
                    if task not in lib.tasks():
                        continue
                    name = f"{lib.id}/{task}/{wid}"
                    if name in a.skip:
                        continue
                    put({"type": "begin", "cell": name})
                    rec = run_cell(lib, task, wid, a)
                    rec.setdefault("input_bytes", len(w.input))
                    put(rec)


def main(argv) -> int:
    a = parse_args(argv)
    try:
        if a.cmd == "list":
            for l in sorted(all_libraries(), key=lambda l: l.id):
                print(l.id, l.dist, version(l), l.role)
        else:
            if not a.out:
                raise ValueError("--out is required")
            run(a)
    except (OSError, ValueError, KeyError, SystemExit) as e:
        print(f"{NAME}: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
