"""Reasons for competitor cells that have no number.

A competitor's `differs` or `rejects` cell can look like a fault of the benchmark. Each such cell
gets a numbered note that gives the first difference the correctness guard found (from the result
itself) and, where the kit knows the behaviour behind it, a short reason from `reasons.json`.

Rules this module keeps:
  * only competitor cells are explained. A cell of our own library, or of a control, stays a plain
    failure with no note and no reason;
  * a reason is chosen by library, task and a pattern on the first difference, so it can only
    explain a difference of the kind it names;
  * a reason that no cell of a full run uses is reported as stale, never kept silently.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DATA = Path(__file__).with_name("reasons.json")


def harness_languages() -> set[str]:
    """The languages the kit has a benchmark harness for: a directory of bench/ with a harness.json."""
    return {p.parent.name for p in Path(__file__).resolve().parents[1].glob("*/harness.json")}
EXPLAINED = ("differs", "rejects", "unjudged", "error")
# Statuses a curated reason may explain. `unjudged` and `error` are the harness's own limits, so
# they only ever show the detail.
CURATED = ("differs", "rejects")
KEYS = {"libraries", "tasks", "status", "detail", "reason"}
CLIP = 200


class ReasonError(Exception):
    pass


def load(path: Path | None = None) -> list[dict]:
    """The curated entries, checked: known keys, a library and a task list, a pattern that compiles."""
    raw = json.loads((path or DATA).read_text(encoding="utf-8"))
    entries = raw.get("entries")
    if not isinstance(entries, list):
        raise ReasonError("reasons file has no entries list")
    for i, e in enumerate(entries):
        where = f"entry {i}"
        if set(e) - KEYS:
            raise ReasonError(f"{where}: unknown keys {sorted(set(e) - KEYS)}")
        for key in ("libraries", "tasks"):
            if not e.get(key) or not all(isinstance(x, str) for x in e[key]):
                raise ReasonError(f"{where}: {key} must be a non-empty list of names")
        if not isinstance(e.get("reason"), str) or not e["reason"].strip():
            raise ReasonError(f"{where}: reason is empty")
        if not set(e.get("status", CURATED)) <= set(CURATED):
            raise ReasonError(f"{where}: status must be among {CURATED}")
        # A reason explains one kind of difference, so it needs a pattern on the first difference, and a
        # pattern that matches the empty text would explain every cell of its library and task.
        if not isinstance(e.get("detail"), str) or not e["detail"]:
            raise ReasonError(f"{where}: detail pattern is required")
        try:
            if re.search(e["detail"], ""):
                raise ReasonError(f"{where}: detail pattern matches the empty text")
        except re.error as err:
            raise ReasonError(f"{where}: bad detail pattern: {err}") from None
        from .report import TASK_ORDER  # imported here: report imports this module
        unknown = set(e["tasks"]) - set(TASK_ORDER)
        if unknown:
            raise ReasonError(f"{where}: unknown tasks {sorted(unknown)}")
    return entries


def eligible(c: dict) -> bool:
    return c["role"] == "competitor" and c["status"] in EXPLAINED


def match_index(entries: list[dict], c: dict) -> int | None:
    """Index of the first entry that explains this cell, or None."""
    if not eligible(c) or c["status"] not in CURATED:
        return None
    for i, e in enumerate(entries):
        if (c["library"] in e["libraries"] and c["task"] in e["tasks"]
                and c["status"] in e.get("status", CURATED)
                and re.search(e.get("detail", ""), c.get("detail") or "")):
            return i
    return None


def stale(result: dict, entries: list[dict] | None = None, languages: set[str] | None = None) -> list[dict]:
    """Entries that no cell of the run uses although the run exercised their library and task. Only a
    full run is judged: a quick run leaves out workloads, so an unused entry proves nothing there."""
    if result["run"]["mode"] != "full":
        return []
    entries = load() if entries is None else entries
    cells = [c for c in result["cells"] if c["role"] == "competitor"]
    used = {match_index(entries, c) for c in cells}
    # When the run has a result for every language the kit benchmarks, a library id that no harness lists
    # is a typo or a removed library, so the entry can never apply.
    listed = {l["id"] for h in result["harnesses"].values() for l in h["libraries"]}
    whole = set(result["harnesses"]) >= (harness_languages() if languages is None else languages)
    return [e for i, e in enumerate(entries) if i not in used
            and (any(c["library"] in e["libraries"] and c["task"] in e["tasks"] for c in cells)
                 or (whole and not set(e["libraries"]) <= listed))]


def stale_lines(bad: list[dict]) -> list[str]:
    return [f"{', '.join(e['libraries'])} / {', '.join(e['tasks'])}: {e['reason']}" for e in bad]


def one_line(text: str) -> str:
    text = (text or "").replace("\n", " ").replace("\r", " ").replace("\t", " ").replace("`", "'")
    return text if len(text) <= CLIP else text[:CLIP - 1] + "…"


class Notes:
    """Numbered notes for the competitor cells without a number in a set of cells. `marker` gives the
    mark to put after the cell's status, `lines` the notes to print once under the tables."""

    def __init__(self, cells: list[dict], entries: list[dict] | None = None):
        entries = load() if entries is None else entries
        groups: dict[tuple, list[dict]] = {}
        self.reason: dict[tuple, str | None] = {}
        for c in cells:
            if not eligible(c):
                continue
            i = match_index(entries, c)
            reason = None if i is None else entries[i]["reason"]
            # A cell with no curated reason is its own note: its first difference is all there is to say.
            key = (c["language"], c["library"], reason if reason else (c["task"], c["status"], c.get("detail")))
            groups.setdefault(key, []).append(c)
            self.reason[key] = reason
        self.groups = sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], str(kv[0][2])))
        self.number: dict[tuple, int] = {}
        for n, (key, members) in enumerate(self.groups, 1):
            for c in members:
                self.number[ident(c)] = n

    def marker(self, c: dict) -> str:
        n = self.number.get(ident(c))
        return f" [{n}]" if n else ""

    def lines(self) -> list[str]:
        out = []
        for n, (key, members) in enumerate(self.groups, 1):
            first = members[0]
            tasks: dict[str, int] = {}
            for c in members:
                tasks[c["task"]] = tasks.get(c["task"], 0) + 1
            where = ", ".join(f"{t} ×{k}" for t, k in tasks.items())
            statuses = "/".join(sorted({c["status"] for c in members}))
            head = f"{n}. `{first['library']}` ({statuses}; {where}): "
            reason = self.reason[key]
            body = (reason + " " if reason else "") + \
                f"What the guard found, in {first['task']} on `{first['workload']}`: `{one_line(first.get('detail'))}`"
            out.append(head + body)
        return out


def ident(c: dict) -> tuple:
    return (c["language"], c["library"], c["task"], c["workload"])
