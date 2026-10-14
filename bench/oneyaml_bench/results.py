"""Result format, ratios, verdicts and the publishability rule.

Everything here is plain functions over plain data so the self-tests can pin each rule.
A result file is one JSON object (see docs/benchmarks.md):

    {"format": "oneyaml-kit-bench/1", "run": {...}, "harnesses": {...},
     "workloads": [...], "cells": [...]}

A cell is one (language, library, task, workload) with a status:

    ok           the guard accepted the output; time and memory are present
    differs      the library produced a different answer; no numbers
    rejects      the library refused an input the specification accepts; no numbers
    unsupported  the library has no such capability; no numbers
    unjudged     the harness could not decide whether the answer is right (the reference
                 parsers disagree about it); no numbers, and not charged to the library
    error        the harness could not measure it (crash, panic); no numbers
"""

from __future__ import annotations

FORMAT = "oneyaml-kit-bench/1"
STATUSES = ("ok", "differs", "rejects", "unsupported", "unjudged", "error")
NON_NUMBER = ("differs", "rejects", "unsupported", "unjudged", "error")

# A difference smaller than this fraction of the time is called a tie whatever the
# spread says: a single-digit-percent gap on a shared host is not a result.
TIME_FLOOR = 0.02
# How many median absolute deviations of noise a gap must exceed to count.
NOISE_MADS = 3.0
# Heap figures are deterministic, so only rounding-level gaps are ties.
MEMORY_FLOOR = 0.01

# The publishability rule. A run is labelled publishable only when every condition
# holds; otherwise the report says why it is a smoke result.
PUBLISH_RULE = {
    "mode": "full",
    # Cores busy with work that is not the harness: the 95th percentile and the worst
    # sample over the whole run, sampled about every half second.
    "foreign_busy_cores_p95_max": 1.0,
    "foreign_busy_cores_max": 4.0,
    # The one-minute load average read before and after the run, with the harness's own
    # single core allowed for.
    "load1_max": 3.0,
    "min_load_samples": 5,
    # Timings are only comparable between runs on one core at a fixed frequency policy.
    "pinned": True,
    "governors": ["performance"],
}


class ResultError(Exception):
    pass


# ---- ratios and verdicts -----------------------------------------------------------------

def ratio(value: float, reference: float) -> float:
    """value / reference: how many times larger the value is. Below 1.0 is better for both
    time and memory."""
    if reference <= 0:
        raise ResultError("a ratio needs a positive reference")
    return value / reference


def rel_spread(median: float, mad: float) -> float:
    return 0.0 if median <= 0 else mad / median


def time_verdict(own_median: float, own_mad: float, other_median: float, other_mad: float) -> str:
    """`win`, `loss` or `tie` for the first library against the second, on time."""
    r = ratio(own_median, other_median)
    noise = max(TIME_FLOOR, NOISE_MADS * max(rel_spread(own_median, own_mad),
                                             rel_spread(other_median, other_mad)))
    if r < 1 - noise:
        return "win"
    if r > 1 + noise:
        return "loss"
    return "tie"


def memory_verdict(own_bytes: float, other_bytes: float) -> str:
    r = ratio(own_bytes, other_bytes)
    if r < 1 - MEMORY_FLOOR:
        return "win"
    if r > 1 + MEMORY_FLOOR:
        return "loss"
    return "tie"


def cell_key(c: dict) -> tuple:
    return (c["language"], c["task"], c["workload"])


def ok_cells(cells: list[dict]) -> list[dict]:
    return [c for c in cells if c["status"] == "ok"]


# ---- what may define "the best" ---------------------------------------------------------------

# Only a cell that did the full task, as the task table in docs/benchmarks.md specifies it, may set
# the reference that other cells are compared with. A cell can fail that in two ways. Its variant says
# the answer was judged more loosely than the plain one (`bytes-only`: no value to compare;
# `value-only`: the text could not be kept; `harness-rendered`: the harness, not the library, wrote
# the text), or its library does not start from the specified state (`state` is `text`: it takes and
# returns text, so parsing and writing are inside the timed work). Such cells are shown, marked,
# and never the best.
#
# `merged` is a library reading YAML 1.1 merge keys while it loads. The value is otherwise the
# expected one and the work is the work of loading the document, so it may set the reference; the
# report labels it wherever it appears.
REFERENCE_VARIANTS = ("spec", "merged")


def non_reference_label(c: dict) -> str:
    """Why a cell may not set the best of its row; "" when it may."""
    why = []
    if c.get("state") == "text":
        why.append("text API")
    v = c.get("variant") or "spec"
    if v not in REFERENCE_VARIANTS:
        why.append(v)
    return ", ".join(why)


def sets_reference(c: dict) -> bool:
    return c["status"] == "ok" and not non_reference_label(c)


def compare_rows(cells: list[dict]) -> list[dict]:
    """One row per (task, workload) with every library's cell, the best competitor and the
    verdicts of each oneyaml library against it.

    The reference for ratios is the best competitor in that row, on that metric: the
    fastest for time, the smallest peak for memory, whoever they are, among the cells that did
    the full task (see `sets_reference`). A row where no competitor did has no reference and
    says so.
    """
    rows: dict[tuple, dict] = {}
    for c in cells:
        rows.setdefault(cell_key(c), {"task": c["task"], "workload": c["workload"], "cells": []})["cells"].append(c)
    out = []
    for row in rows.values():
        cs = row["cells"]
        comp = [c for c in cs if c["role"] == "competitor" and sets_reference(c)]
        row["best_time"] = min(comp, key=lambda c: c["time"]["median_ns"]) if comp else None
        row["best_memory"] = min(comp, key=lambda c: c["memory"]["peak_bytes"]) if comp else None
        row["own"] = []
        for c in cs:
            if c["role"] != "own":
                continue
            entry = {"library": c["library"], "language": c["language"], "status": c["status"]}
            if c["status"] != "ok":
                entry.update(time=None, memory=None)
            else:
                t, m = c["time"], c["memory"]
                bt, bm = row["best_time"], row["best_memory"]
                entry["time"] = None if bt is None else {
                    "ratio": ratio(t["median_ns"], bt["time"]["median_ns"]),
                    "vs": bt["library"],
                    "verdict": time_verdict(t["median_ns"], t["mad_ns"], bt["time"]["median_ns"], bt["time"]["mad_ns"]),
                }
                entry["memory"] = None if bm is None else {
                    "ratio": ratio(max(m["peak_bytes"], 1), max(bm["memory"]["peak_bytes"], 1)),
                    "vs": bm["library"],
                    "verdict": memory_verdict(max(m["peak_bytes"], 1), max(bm["memory"]["peak_bytes"], 1)),
                }
            row["own"].append(entry)
        out.append(row)
    return out


def tally(rows: list[dict], metric: str) -> dict:
    """Counts of wins, ties, losses, and rows the own library has no number or no rival for."""
    t = {"win": 0, "tie": 0, "loss": 0, "no-number": 0, "no-rival": 0}
    for r in rows:
        for e in r["own"]:
            if e["status"] != "ok":
                t["no-number"] += 1
            elif e[metric] is None:
                t["no-rival"] += 1
            else:
                t[e[metric]["verdict"]] += 1
    return t


# ---- publishability ----------------------------------------------------------------------

def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def assess_publishability(mode: str, samples: list[float], load1_start: float, load1_end: float,
                          complete: bool, selftest_build: bool = False, pinned_cpu: int | None = None,
                          governor: str = "unknown") -> dict:
    """Decides whether a run may carry numbers into a README.

    `samples` are the cores busy with other work, one per sampling tick.
    """
    reasons = []
    rule = PUBLISH_RULE
    if mode != rule["mode"]:
        reasons.append(f"the run was a {mode} run; only a full run can be published")
    if selftest_build:
        reasons.append("the harness was built with its self-test controls")
    if not complete:
        reasons.append("the run did not finish every cell it set out to measure")
    if len(samples) < rule["min_load_samples"]:
        reasons.append(f"the host load was sampled only {len(samples)} times, fewer than {rule['min_load_samples']}")
    if rule["pinned"] and pinned_cpu is None:
        reasons.append("the harness was not pinned to one CPU, so the scheduler may move it between cores "
                       "(use --pin)")
    if governor not in rule["governors"]:
        reasons.append(f"the CPU frequency governor is {governor!r}, not one of {rule['governors']}: "
                       "clock speed can change during the run")
    p95 = percentile(samples, 0.95)
    worst = max(samples) if samples else 0.0
    if p95 > rule["foreign_busy_cores_p95_max"]:
        reasons.append(f"other work kept {p95:.1f} cores busy for the slowest 5% of the run "
                       f"(limit {rule['foreign_busy_cores_p95_max']})")
    if worst > rule["foreign_busy_cores_max"]:
        reasons.append(f"other work peaked at {worst:.1f} busy cores (limit {rule['foreign_busy_cores_max']})")
    for name, v in (("start", load1_start), ("end", load1_end)):
        if v > rule["load1_max"]:
            reasons.append(f"the one-minute load average was {v:.1f} at the {name} of the run "
                           f"(limit {rule['load1_max']})")
    return {
        "publishable": not reasons,
        "label": "publishable" if not reasons else "smoke",
        "reasons": reasons,
        "rule": dict(rule),
        "measured": {"foreign_busy_cores_p95": round(p95, 2), "foreign_busy_cores_max": round(worst, 2),
                     "load1_start": load1_start, "load1_end": load1_end, "load_samples": len(samples),
                     "pinned_cpu": pinned_cpu, "governor": governor},
    }


def add_reasons(result: dict, reasons: list[str]) -> None:
    """Marks a result as not publishable for reasons found after the run (a wrong list of
    known deviations, say). A report built from it says why."""
    if not reasons:
        return
    pub = result["run"]["publishability"]
    pub["reasons"] = list(pub["reasons"]) + list(reasons)
    pub["publishable"] = False
    pub["label"] = "smoke"


def attach_known(result: dict, known: dict[str, dict]) -> None:
    """Copies the reason of each listed known deviation onto its cell, so the report, which is built
    from the result alone, can say why a library has no number. `known` maps a language to its
    parsed known-deviations file."""
    for c in result["cells"]:
        entries = {e["cell"]: e for e in known.get(c["language"], {}).get("entries", [])}
        e = entries.get(cell_name(c))
        if e is not None and e.get("reason"):
            c["known"] = e["reason"]


# ---- validation --------------------------------------------------------------------------

def validate(result: dict) -> None:
    """Raises ResultError unless the result obeys the format. The report generator runs
    this first, so a hand-edited or truncated file cannot produce a table."""
    if result.get("format") != FORMAT:
        raise ResultError(f"format must be {FORMAT!r}")
    for key in ("run", "harnesses", "workloads", "cells"):
        if key not in result:
            raise ResultError(f"missing {key}")
    run = result["run"]
    for key in ("date", "mode", "environment", "publishability"):
        if key not in run:
            raise ResultError(f"run is missing {key}")
    if run["mode"] not in ("quick", "full"):
        raise ResultError("run.mode must be quick or full")
    pub = run["publishability"]
    if pub["publishable"] and (pub["reasons"] or run["mode"] != "full"):
        raise ResultError("a run with reasons against it, or a quick run, cannot be publishable")
    seen = set()
    for c in result["cells"]:
        for key in ("language", "library", "role", "task", "workload", "status"):
            if key not in c:
                raise ResultError(f"cell is missing {key}: {c}")
        ident = (c["language"], c["library"], c["task"], c["workload"])
        if ident in seen:
            raise ResultError(f"duplicate cell {ident}")
        seen.add(ident)
        if c["status"] not in STATUSES:
            raise ResultError(f"bad status {c['status']!r} in {ident}")
        has_numbers = "time" in c or "memory" in c
        if c["status"] == "ok":
            for k, fields in (("time", ("median_ns", "mad_ns", "samples")),
                              ("memory", ("method", "peak_bytes", "allocated_bytes", "allocations"))):
                if k not in c or any(f not in c[k] for f in fields):
                    raise ResultError(f"ok cell {ident} lacks {k}")
            if c["time"]["median_ns"] <= 0:
                raise ResultError(f"cell {ident} has a non-positive time")
            r = c["memory"].get("retained_bytes")
            if r is not None and (not isinstance(r, int) or r < 0):
                raise ResultError(f"cell {ident} has a bad retained_bytes")
        elif has_numbers:
            # The rule that matters most: a wrong or missing answer never carries a number.
            raise ResultError(f"cell {ident} is {c['status']} but carries numbers")
        elif c["status"] in ("differs", "rejects", "unjudged", "error") and not c.get("detail"):
            raise ResultError(f"cell {ident} is {c['status']} without a reason")



# ---- known deviations ----------------------------------------------------------------------

def cell_name(c: dict) -> str:
    return f"{c['library']}/{c['task']}/{c['workload']}"


# The only variant that is not a deviation: the plain expectation. `merged` (a library that
# applies `<<`) and `bytes-only` (a document judged on its bytes because the library gives no
# value to compare) are accepted but listed.
PLAIN_VARIANTS = ("spec",)


def deviation_of(c: dict) -> tuple[str, str] | None:
    """(status, variant) when a cell is anything other than a plain correct answer.

    Unsupported cells are a library's declared capabilities, not deviations.
    """
    if c["status"] == "unsupported" or c["role"] == "control":
        return None
    if c["status"] == "ok":
        v = c.get("variant") or "spec"
        return None if v in PLAIN_VARIANTS else ("ok", v)
    return (c["status"], "")


def verify_known(cells: list[dict], known: dict, language: str, universe: dict | None = None,
                 libraries: set[str] | None = None) -> list[str]:
    """Problems between the cells measured and the list of known deviations.

    A deviation nobody listed is a new failure; a listed one that no longer happens is a
    stale entry. Both fail the run, so the list stays true.

    `universe` maps every workload id (all tiers) to the tasks it defines, and `libraries`
    is every library of the language. An entry that names a library, task or workload
    outside them, or a task the workload does not define, is stale whatever was measured.
    Only an entry for a real cell that this run did not measure (another tier, a filter) is
    left alone.
    """
    problems = []
    entries = {}
    for e in known.get("entries", []):
        if e["cell"] in entries:
            problems.append(f"known deviation {e['cell']} is listed twice")
        entries[e["cell"]] = e
    for name, e in entries.items():
        if not e.get("reason"):
            problems.append(f"known deviation {name} has no reason")
        parts = name.split("/", 2)
        if len(parts) != 3:
            problems.append(f"known deviation {name} is not library/task/workload")
        elif universe is not None:
            lib, task, wl = parts
            if libraries is not None and lib not in libraries:
                problems.append(f"stale known deviation {name}: no library {lib!r}")
            elif wl not in universe:
                problems.append(f"stale known deviation {name}: no workload {wl!r}")
            elif task not in universe[wl]:
                problems.append(f"stale known deviation {name}: workload {wl!r} has no task {task!r}")
    measured = {cell_name(c): c for c in cells if c["language"] == language}
    for name, c in measured.items():
        dev = deviation_of(c)
        e = entries.get(name)
        if dev and not e:
            why = c.get("detail") or dev[1]
            problems.append(f"unlisted deviation {name}: {dev[0]} {dev[1]} {why}".replace("  ", " ")[:200])
        elif dev and e and (e["status"], e.get("variant", "")) != dev:
            problems.append(f"{name} is listed as {e['status']} {e.get('variant', '')} but is "
                            f"{dev[0]} {dev[1]}".replace("  ", " "))
    for name, e in entries.items():
        c = measured.get(name)
        if c is not None and deviation_of(c) is None:
            problems.append(f"stale known deviation {name}: it is now {c['status']}")
    return problems
