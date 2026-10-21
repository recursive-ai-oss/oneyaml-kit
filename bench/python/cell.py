"""One cell: judge the library's answer, and only if it is right, time it and measure its memory.

Run as `python cell.py --workloads DIR --library ID --task T --workload W --cell-out FILE [pyperf options]`.
This process is pyperf's master; pyperf starts its workers from this same script, and a worker skips
the judging (it already passed in the master) and builds the very same runner to time.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pyperf

# The harness leaves nothing behind in the checkout, and pyperf starts its workers without the caller's environment.
sys.dont_write_bytecode = True

import guard
import measure
from common import clip
from referee import two_referees
from registry import build_runner

HERE = Path(__file__).resolve().parent
KEYS = ("workloads", "library", "task", "workload", "cell-out")


def add_args(cmd, args):
    for k in KEYS:
        cmd += ["--" + k, getattr(args, k.replace("-", "_"))]


def check(runner, task, w):
    """Runs the task once as it is timed and judges the whole answer."""
    error = product = failing = None
    try:
        product = runner.check()
        failing = runner.failing() if runner.failing else None
    except Exception as e:  # noqa: BLE001 - whatever the library raises is the library refusing the input
        error = e
    return guard.judge(task, w, product, error, failing, two_referees)


MEMORY_RUNS = 3


def memory_of(args) -> int:
    """The median over MEMORY_RUNS fresh processes of the peak resident-set growth of one run (see measure.py):
    the figure of a native library moves by tens of percent between processes, a median much less. The
    processes run side by side; nothing is being timed."""
    cmd = [sys.executable, str(HERE / "memchild.py"), "--workloads", args.workloads, "--library", args.library,
           "--task", args.task, "--workload", args.workload]
    env = {**os.environ, "PYTHONMALLOC": "malloc"}
    procs = [subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
             for _ in range(MEMORY_RUNS)]
    sizes = []
    for p in procs:
        out, err = p.communicate()
        if p.returncode != 0:
            last = err.strip().splitlines()[-1:] or [f"exit status {p.returncode}"]
            raise RuntimeError("the memory run failed: " + clip(re.sub(r"/[^\s:']+", "…", last[0])))
        sizes.append(int(json.loads(out)["bytes"]))
    return sorted(sizes)[MEMORY_RUNS // 2]


def main() -> None:
    runner = pyperf.Runner(add_cmdline_args=add_args)
    ap = runner.argparser
    for k in KEYS:
        ap.add_argument("--" + k, required=True)
    args = runner.parse_args()
    w, r, why = build_runner(args.workloads, args.library, args.task, args.workload)
    rec = {}
    if not args.worker:
        rec = {"type": "cell", "library": args.library, "task": args.task, "workload": w.id,
               "input_bytes": len(w.input)}
        if r is None:
            rec.update(status="unsupported", detail=why)
        else:
            v = check(r, args.task, w)
            rec.update(status=v.status, detail=v.detail)
            if v.status == "ok":
                rec["variant"] = v.variant
        if rec["status"] != "ok":
            Path(args.cell_out).write_text(json.dumps(rec), encoding="utf-8")
            return
    if not args.worker and not args.loops:
        # Workers are told the loops by the master, so only the master decides.
        args.loops = measure.capped_loops(r, args.min_time)
    bench = runner.bench_time_func(f"{args.library}/{args.task}/{args.workload}", measure.time_func, r)
    if args.worker:
        return
    rec["time"] = {"ns": bench.mean() * 1e9, "stat": "mean", "median_ns": bench.median() * 1e9,
                   "stdev_ns": bench.stdev() * 1e9, "values": len(bench.get_values()),
                   "loops": bench.get_loops(), "processes": bench.get_nrun()}
    try:
        rec["memory"] = {"allocated_bytes": memory_of(args), "allocations": None}
    except (RuntimeError, OSError, ValueError) as e:
        rec.update(status="error", detail=f"memory: {e}")
        rec.pop("time", None)
        rec.pop("variant", None)
    Path(args.cell_out).write_text(json.dumps(rec), encoding="utf-8")


if __name__ == "__main__":
    main()
