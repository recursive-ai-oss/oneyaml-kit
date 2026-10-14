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
    error        the harness could not measure it (crash, panic); no numbers
"""

from __future__ import annotations

FORMAT = "oneyaml-kit-bench/1"
STATUSES = ("ok", "differs", "rejects", "unsupported", "error")
NON_NUMBER = ("differs", "rejects", "unsupported", "error")

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


def compare_rows(cells: list[dict]) -> list[dict]:
    """One row per (task, workload) with every library's cell, the best competitor and the
    verdicts of each oneyaml library against it.

    The reference for ratios is the best competitor in that row, on that metric: the
    fastest for time, the smallest peak for memory, whoever they are. A row where no
    competitor produced a correct answer has no reference and says so.
    """
    rows: dict[tuple, dict] = {}
    for c in cells:
        rows.setdefault(cell_key(c), {"task": c["task"], "workload": c["workload"], "cells": []})["cells"].append(c)
    out = []
    for row in rows.values():
        cs = row["cells"]
        comp = [c for c in ok_cells(cs) if c["role"] == "competitor"]
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
                          complete: bool, selftest_build: bool = False) -> dict:
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
                     "load1_start": load1_start, "load1_end": load1_end, "load_samples": len(samples)},
    }


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
            for k, fields in (("time", ("median_ns", "mad_ns", "samples")), ("memory", ("peak_bytes", "allocated_bytes", "allocations"))):
                if k not in c or any(f not in c[k] for f in fields):
                    raise ResultError(f"ok cell {ident} lacks {k}")
            if c["time"]["median_ns"] <= 0:
                raise ResultError(f"cell {ident} has a non-positive time")
        elif has_numbers:
            # The rule that matters most: a wrong or missing answer never carries a number.
            raise ResultError(f"cell {ident} is {c['status']} but carries numbers")
        elif c["status"] in ("differs", "rejects", "error") and not c.get("detail"):
            raise ResultError(f"cell {ident} is {c['status']} without a reason")



# ---- known deviations ----------------------------------------------------------------------

def cell_name(c: dict) -> str:
    return f"{c['library']}/{c['task']}/{c['workload']}"


# Variants that are not deviations: the plain expectation, and a document judged on its bytes
# alone because the library has no data model to read a value from.
PLAIN_VARIANTS = ("spec", "bytes-only")


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


def verify_known(cells: list[dict], known: dict, language: str) -> list[str]:
    """Problems between the cells measured and the list of known deviations.

    A deviation nobody listed is a new failure; a listed one that no longer happens is a
    stale entry. Both fail the run, so the list stays true. A listed cell that was not
    measured in this run (another tier, a filter) is left alone.
    """
    problems = []
    entries = {e["cell"]: e for e in known.get("entries", [])}
    for name, e in entries.items():
        if not e.get("reason"):
            problems.append(f"known deviation {name} has no reason")
    measured = {cell_name(c): c for c in cells if c["language"] == language}
    for name, c in measured.items():
        dev = deviation_of(c)
        e = entries.get(name)
        if dev and not e:
            why = c.get("detail") or dev[1]
            problems.append(f"unlisted deviation {name}: {dev[0]} {dev[1]} {why}".rstrip()[:200])
        elif dev and e and (e["status"], e.get("variant", "")) != dev:
            problems.append(f"{name} is listed as {e['status']} {e.get('variant', '')} but is {dev[0]} {dev[1]}".replace('  ', ' '))
    for name, e in entries.items():
        c = measured.get(name)
        if c is not None and deviation_of(c) is None:
            problems.append(f"stale known deviation {name}: it is now {c['status']}")
    return problems
