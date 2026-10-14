#!/usr/bin/env python3
"""Writes bench/go/known.json from a results file.

    bench/go/record_known.py out/bench/results.json

Every cell of the Go harness that is not a plain correct answer (differs, rejects, unjudged,
error, or an accepted variant other than the plain one) is listed with a reason. The reason is
chosen from what the cell's own detail says, and the harness's detail names the cause: the kinds
of difference between two texts (blank lines, indentation, ...) or the first line that differs.
A cell whose detail matches no known pattern stops the script, so a new kind of failure is read
by a person before it is listed. The run itself fails when a listed cell no longer happens or an
unlisted one does.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "tools"))
sys.dont_write_bytecode = True

from oneyaml_kit.public_text import PATTERN  # noqa: E402

KINDS = {
    "blank lines": "blank lines",
    "indentation": "indentation",
    "quoting": "quoting",
    "trailing spaces": "trailing spaces",
    "spacing inside lines": "spacing inside lines",
    "line breaks": "line breaks",
}


def safe(text: str) -> str:
    """Quoted data from a case can look like internal text to the public-text check; the reason then
    keeps its words and leaves the data out."""
    return "(the line is in the cell's detail)" if PATTERN.search(text) else text


def text_cause(detail: str, what: str) -> str | None:
    """The cause of a difference between two texts, from the end of the harness's detail."""
    m = re.search(r"; the texts differ only in (.+)$", detail)
    if m:
        return f"{what} differs from the expected text only in {m.group(1)}."
    m = re.search(r'; line (\d+): got (".*"), expected (".*")$', detail)
    if m:
        got, want = m.group(2), m.group(3)
        if got == '""' or want == '""':
            return f"{what} differs from the expected text first at line {m.group(1)}, where one has a line and the other is empty."
        return safe(f"{what} differs from the expected text first at line {m.group(1)}: it has {got} where {want} was expected.")
    m = re.search(r"; got (\d+) lines, expected (\d+)$", detail)
    if m:
        return f"{what} has {m.group(1)} lines where {m.group(2)} were expected."
    return None


def reason(c: dict) -> str | None:
    s, task, v = c["status"], c["task"], c.get("variant") or "spec"
    d = c.get("detail") or ""
    if s == "ok":
        if v == "merged":
            return ("The library applies `<<` merge keys while loading, which YAML 1.2 does not; the value "
                    "is accepted as the merged one.")
        if v == "value-only":
            return ("The library works on JSON, so it cannot keep the text; the value it writes is the "
                    "expected one and is accepted as value-only.")
        return None
    if s == "error":
        if "disagree" in d:
            return ("The allocation figures of the memory pass and of the timed runs differ by more than "
                    "the check allows (the library keeps buffers in a pool, so bytes depend on collections).")
        return None
    if s == "unjudged":
        return "The two reference parsers read the library's output differently, so it cannot be judged."
    if s == "rejects":
        if "missing path" in d or "does not apply" in d:
            return ("The library reads the YAML 1.1 key `on` as a boolean, so the path /on does not exist "
                    "in the JSON it patches.")
        return ("The library refused an input that YAML 1.2 accepts: " + d) if d else None
    if s != "differs":
        return None
    # -- values -----------------------------------------------------------------------------
    if task == "patch" and d.startswith("value after the patch") and "(float)" in d:
        return ("The patched value differs: the library goes through JSON, so a float with a whole "
                "value comes back as an integer.")
    if re.search(r"got -?\d+, expected -?\d+(\.\d+)? \(float\)", d) and task == "dump":
        return "A float with a whole value is written without a decimal point, so it reads back as an integer."
    if re.search(r'got -?\d+, expected "[0-9]+"', d) and task == "dump":
        return ("A string of digits with a leading zero is written as a plain scalar, which the core schema "
                "reads as an integer.")
    if "(float), expected" in d:
        return "An integer comes back as a float: the library converts to JSON and reads numbers as floats."
    if "missing /on" in d or (task == "typed-load" and d.startswith("/on/")):
        if task == "typed-load":
            return "The library reads the YAML 1.1 key `on` as the boolean true, so the model's `on` field stays empty."
        return "The library reads the YAML 1.1 key `on` as the boolean true, not as the string \"on\"."
    if re.search(r'got true, expected "', d):
        return "The library reads the YAML 1.1 scalar `y` as the boolean true."
    if "expected a sequence of" in d and "got a mapping" in d:
        return "The library reads only the first document of a stream."
    if task in ("generic-load", "dump", "typed-load"):
        return None
    # -- texts ------------------------------------------------------------------------------
    if task == "document-parse" or task == "unchanged-emit":
        return text_cause(d, "The document written back")
    if task == "edit-script":
        return text_cause(d, "The edited document")
    if task == "patch":
        return text_cause(d, "The patched document")
    return None


def main(argv: list[str]) -> int:
    res = json.loads(Path(argv[0]).read_bytes())
    entries, unknown = [], []
    for c in sorted(res["cells"], key=lambda c: (c["library"], c["task"], c["workload"])):
        if c["language"] != "go" or c["status"] == "unsupported":
            continue
        if c["status"] == "ok" and (c.get("variant") or "spec") == "spec":
            continue
        why = reason(c)
        if why is None:
            unknown.append(f"{c['library']}/{c['task']}/{c['workload']}: {c['status']} {c.get('detail', '')[:200]}")
            continue
        e = {"cell": f"{c['library']}/{c['task']}/{c['workload']}", "status": c["status"]}
        if c["status"] == "ok":
            e["variant"] = c["variant"]
        e["reason"] = why
        entries.append(e)
    if unknown:
        print("cells with no reason pattern:\n  " + "\n  ".join(unknown), file=sys.stderr)
        return 1
    doc = {"format": "oneyaml-kit-bench-known/1", "language": "go", "entries": entries}
    (HERE / "known.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(entries)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
