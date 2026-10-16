"""Result format, ratios and verdicts.

A result file is one JSON object (see docs/benchmarks.md):

    {"format": "oneyaml-kit-bench/2", "run": {...}, "harnesses": {...},
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

import json
from pathlib import Path

FORMAT = "oneyaml-kit-bench/2"
STATUSES = ("ok", "differs", "rejects", "unsupported", "unjudged", "error")

# A gap smaller than this is called a tie. Each language's own tool gives one figure per cell
# and a shared machine moves it by a few percent, so a narrower gap is not a result.
TIME_BAND = 0.05
MEMORY_BAND = 0.02
# YamlDotNet's generic-load allocation takes one of a few distinct amounts per run, about 5.6% apart
# and up to about 12% apart in all (quick mode, repeated runs). Every other library repeated within
# about 1.2%. A comparison involving a library listed here uses the wider band.
MEMORY_BANDS = {"yamldotnet": 0.12}
# A memory gap smaller than this many bytes is a tie, whatever the ratio, when a library that is
# listed in MEMORY_FLOORS is in the comparison. On .NET cells under 1 KB the figure moves between
# processes by a few bytes, which is a large ratio on a cell that small. Repeated quick runs differed by up to 22 bytes for our .NET
# library, 6 for SharpYaml's syntax reader and 3 for VYaml; 64 bytes is about three times the largest.
# Rust allocation counts repeated exactly (no spread over 35 repeated cells), and Go's repeat closely,
# so they have no floor: there a small gap is a result. Above the floor the relative band decides.
MEMORY_FLOOR_BYTES = 64
MEMORY_FLOORS = {lib: MEMORY_FLOOR_BYTES
                 for lib in ("oneyaml-csharp", "oneyaml-csharp-gen", "sharpyaml-syntax", "vyaml")}


def memory_band(*libraries: str) -> float:
    """The memory band for a comparison: the widest band of the libraries compared."""
    return max(MEMORY_BANDS.get(lib, MEMORY_BAND) for lib in libraries)


def memory_floor(*libraries: str) -> int:
    """The absolute memory floor in bytes for a comparison: the largest floor of the libraries
    compared, and 0 when none of them has one."""
    return max(MEMORY_FLOORS.get(lib, 0) for lib in libraries)


class ResultError(Exception):
    pass


def ratio(value: float, reference: float) -> float:
    """value / reference: how many times larger the value is. Below 1.0 is better."""
    if reference <= 0:
        raise ResultError("a ratio needs a positive reference")
    return value / reference


def verdict(own: float, other: float, band: float, floor: float = 0) -> str:
    """`win`, `loss` or `tie` for the first value against the second; smaller is better. A gap
    smaller than `floor` (in the values' own unit) is a tie whatever the ratio."""
    if abs(own - other) < floor:
        return "tie"
    r = ratio(own, other)
    if r < 1 - band:
        return "win"
    if r > 1 + band:
        return "loss"
    return "tie"


def cell_key(c: dict) -> tuple:
    return (c["language"], c["task"], c["workload"])


def cell_name(c: dict) -> str:
    return f"{c['library']}/{c['task']}/{c['workload']}"


# ---- what is compared with, and what may be called "the best" ---------------------------------

# A row is always compared with the strongest competitor in it: the fastest for time, the least
# allocated for memory. A cell that did not do the full task as docs/benchmarks.md specifies it
# carries a mark: its answer was judged more loosely (`bytes-only`, `value-only`,
# `harness-rendered`), or its library takes and returns text (`state` is `text`).
#
# The mark changes what is said, never whether a loss is shown. A marked cell is never called "the
# best", but when it is faster than ours, or is the only competitor, the verdict is against it and
# names it with its mark, and a loss to it is a loss.
#
# `merged` is a library reading YAML 1.1 merge keys while it loads. The value is otherwise the
# expected one and the work is the work of loading the document, so it is unmarked.
REFERENCE_VARIANTS = ("spec", "merged")

METRICS = {"time": ("time", "ns", TIME_BAND), "memory": ("memory", "allocated_bytes", MEMORY_BAND)}


def non_reference_label(c: dict) -> str:
    """Why a cell carries the mark (and is never called the best of its row); "" when it is plain."""
    why = []
    if c.get("state") == "text":
        why.append("text API")
    v = c.get("variant") or "spec"
    if v not in REFERENCE_VARIANTS:
        why.append(v)
    return ", ".join(why)


def value_of(c: dict, metric: str) -> float:
    group, key, _ = METRICS[metric]
    return max(c[group][key], 1)


def strongest(cells: list[dict], metric: str) -> dict | None:
    """The strongest competitor among the cells with a number, marked or not. A tie goes to the
    unmarked cell, so a marked one is named only when it is really ahead."""
    comp = [c for c in cells if c["role"] == "competitor" and c["status"] == "ok"]
    if not comp:
        return None
    return min(comp, key=lambda c: (value_of(c, metric), bool(non_reference_label(c))))


def best_in_row(row: dict, metric: str) -> dict | None:
    """The cell that a language without a library of ours is read against: the strongest cell that
    did the full task, or the strongest marked one when only marked cells answered."""
    ok = [c for c in row["cells"] if c["role"] != "control" and c["status"] == "ok"]
    pool = [c for c in ok if not non_reference_label(c)] or ok
    return min(pool, key=lambda c: value_of(c, metric)) if pool else None


def compare_rows(cells: list[dict]) -> list[dict]:
    """One row per (language, task, workload) with every library's cell, the strongest competitor and
    the verdicts of each of our libraries against it. A row has no reference only when no competitor
    produced a correct answer."""
    rows: dict[tuple, dict] = {}
    for c in cells:
        rows.setdefault(cell_key(c), {"task": c["task"], "workload": c["workload"], "cells": []})["cells"].append(c)
    out = []
    for row in rows.values():
        cs = row["cells"]
        row["best"] = {m: strongest(cs, m) for m in METRICS}
        row["own"] = []
        for c in cs:
            if c["role"] != "own":
                continue
            entry = {"library": c["library"], "language": c["language"], "status": c["status"]}
            for m, (_, _, base_band) in METRICS.items():
                best = row["best"][m]
                if c["status"] != "ok" or best is None:
                    entry[m] = None
                    continue
                band = memory_band(c["library"], best["library"]) if m == "memory" else base_band
                floor = memory_floor(c["library"], best["library"]) if m == "memory" else 0
                entry[m] = {"ratio": ratio(value_of(c, m), value_of(best, m)), "vs": best["library"],
                            "vs_mark": non_reference_label(best),
                            "verdict": verdict(value_of(c, m), value_of(best, m), band, floor)}
            row["own"].append(entry)
        out.append(row)
    return out


def tally(rows: list[dict], metric: str) -> dict:
    """Counts of wins, ties, losses, and rows our library has no number or no rival for. A loss to a
    marked competitor is a loss; a row has no rival only when no competitor answered correctly."""
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


# ---- partial runs ------------------------------------------------------------------------

def missing_languages(result: dict) -> list[str]:
    """The languages the run was meant to measure and has no results for. A result with any is not a
    complete run."""
    return sorted(set(result["run"].get("expected_languages", [])) - set(result["harnesses"]))


def merge(parts: list[dict]) -> dict:
    """One result from per-language results of the same run."""
    first = parts[0]
    merged = {**first, "run": dict(first["run"]), "harnesses": {}, "cells": []}
    merged["run"]["seconds"] = round(sum(p["run"]["seconds"] for p in parts), 1)
    merged["run"]["load"] = {"before": first["run"]["load"]["before"], "after": parts[-1]["run"]["load"]["after"]}
    merged["run"]["expected_languages"] = sorted({l for p in parts for l in p["run"].get("expected_languages", [])})
    for p in parts:
        merged["harnesses"].update(p["harnesses"])
        merged["cells"] += p["cells"]
    return merged


def load(path) -> dict | None:
    """A result from a file, or from a directory: its results.json, else the per-language results-*.json
    a run cut short leaves. None when there is nothing."""
    path = Path(path)
    if path.is_dir():
        whole = path / "results.json"
        if whole.is_file():
            path = whole
        else:
            files = sorted(path.glob("results-*.json"))
            return merge([json.loads(f.read_text(encoding="utf-8")) for f in files]) if files else None
    return json.loads(path.read_text(encoding="utf-8"))


# ---- validation --------------------------------------------------------------------------

def validate(result: dict) -> None:
    """Raises ResultError unless the result obeys the format. The report generator runs this
    first, so a hand-edited or truncated file cannot produce a table."""
    if result.get("format") != FORMAT:
        raise ResultError(f"format must be {FORMAT!r}")
    for key in ("run", "harnesses", "workloads", "cells"):
        if key not in result:
            raise ResultError(f"missing {key}")
    run = result["run"]
    for key in ("date", "mode", "environment", "smoke", "load"):
        if key not in run:
            raise ResultError(f"run is missing {key}")
    if run["mode"] not in ("quick", "full"):
        raise ResultError("run.mode must be quick or full")
    if run["mode"] == "quick" and not run["smoke"]:
        raise ResultError("a quick run is a smoke run")
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
        if c["status"] == "ok":
            if c.get("time", {}).get("ns", 0) <= 0:
                raise ResultError(f"ok cell {ident} lacks a positive time")
            if "allocated_bytes" not in c.get("memory", {}):
                raise ResultError(f"ok cell {ident} lacks memory")
        elif "time" in c or "memory" in c:
            # The rule that matters most: a wrong or missing answer never carries a number.
            raise ResultError(f"cell {ident} is {c['status']} but carries numbers")
        elif c["status"] in ("differs", "rejects", "unjudged", "error") and not c.get("detail"):
            raise ResultError(f"cell {ident} is {c['status']} without a reason")
