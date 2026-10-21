"""Time and memory of one cell.

Time is pyperf's own: `Runner.bench_time_func` at its defaults (worker processes, warm-up values,
calibrated loops), and quick mode only shortens pyperf's settings. The function pyperf times builds
every starting state outside the clock and drops each result inside it.

Memory is the peak growth of the process's resident set during one run, in a fresh process. Half of these
libraries allocate outside Python's allocator (Rust, C), which tracemalloc cannot see, so the method has to
count what the process holds from the kernel whoever asked for it. To make that figure the work of the
run and not of the process around it, the child (memchild.py) is started with PYTHONMALLOC=malloc, so that
Python objects, C and Rust allocations all come from one heap and no pool or arena of Python's own hides
them; it runs the task once on a tiny document first (the library's one-time costs); it builds the
starting state; it returns the heap's free pages to the kernel; and it reports the peak resident size of
the run minus the resident size before it, and the cell takes the median of three such processes (a native
library moves by tens of percent between processes). Routing Python's allocator through malloc adds a few bytes of
bookkeeping per Python object for every library alike. The kernel counts in pages, so the figure is a
multiple of 4096, and allocation counts are not available by this method.
"""

from __future__ import annotations

import gc
import os
import time

# How many starting states exist at once: about this many bytes of input, so a small document gets many
# (a fast task on it would otherwise be asked for a million, one at a time) and a large one few.
CHUNK_BYTES = 1 << 20
CHUNK_MIN, CHUNK_MAX = 4, 256
# The cyclic collector runs outside the clock after about this many states, so a run does not pay for
# the garbage of the states before it.
GC_EVERY = 1024

# pyperf's settings for quick mode (it does not shorten anything else). Full mode passes none.
QUICK_ARGS = ["--processes", "3", "--values", "2", "--warmups", "1", "--min-time", "0.05"]

# A value may take at most this many times pyperf's minimum time, building starting states included. Every
# run has a state of its own, so a task that takes nanoseconds on a state that takes microseconds to build
# would be asked by pyperf's calibration for as many states as fill the minimum time with the runs alone.
SETUP_BUDGET = 10
MIN_LOOPS = 16


def chunk_for(runner) -> int:
    return max(CHUNK_MIN, min(CHUNK_MAX, CHUNK_BYTES // max(getattr(runner, "input_bytes", 0), 1)))


def time_func(loops: int, runner) -> float:
    """pyperf's time function: the seconds that `loops` runs took, each on a starting state of its own.
    States are built with the clock stopped and released after it; each result is dropped inside it."""
    clock = time.perf_counter
    run, setup, chunk = runner.run, runner.setup, chunk_for(runner)
    total, left, since = 0.0, loops, 0
    gc.collect()
    while left:
        n = min(left, chunk)
        states = [setup() for _ in range(n)]
        since += n
        if since >= GC_EVERY:
            gc.collect()
            since = 0
        out = None
        t0 = clock()
        for st in states:
            out = run(st)
        out = None
        total += clock() - t0
        del states
        left -= n
    return total


def capped_loops(runner, min_time: float) -> int:
    """0 when pyperf's own calibration fits the setup budget, else the number of loops that does. It times a
    few states being built and run: the harness choosing how to measure, not an assertion about speed."""
    clock = time.perf_counter
    probe = 4
    t0 = clock()
    states = [runner.setup() for _ in range(probe)]
    setup_s = (clock() - t0) / probe
    t0 = clock()
    for st in states:
        runner.run(st)
    run_s = (clock() - t0) / probe
    wanted = min_time / max(run_s, 1e-9)
    per_state = setup_s + run_s
    if wanted * per_state <= SETUP_BUDGET * min_time:
        return 0
    return max(int(SETUP_BUDGET * min_time / per_state), MIN_LOOPS)


def _status_kib(field: str) -> int:
    fd = os.open("/proc/self/status", os.O_RDONLY)
    try:
        text = os.read(fd, 8192).decode()
    finally:
        os.close(fd)
    for line in text.splitlines():
        if line.startswith(field + ":"):
            return int(line.split()[1])
    raise OSError(f"no {field} in /proc/self/status")


def trim_heap() -> None:
    """Gives the heap's free pages back to the kernel, so what the process holds before the run is what is live."""
    import ctypes

    ctypes.CDLL("libc.so.6").malloc_trim(0)


def memory_of_one_run(runner) -> int:
    """Bytes of resident-set growth at the peak of one run of a fresh process. Raises OSError where the
    platform cannot reset the peak (anything but Linux)."""
    st = runner.setup()
    gc.collect()
    trim_heap()
    # Writing 5 resets the peak resident size to the current one.
    with open("/proc/self/clear_refs", "w") as f:
        f.write("5")
    before = _status_kib("VmRSS")
    out = runner.run(st)
    peak = _status_kib("VmHWM")
    del out
    return max(peak - before, 0) * 1024
