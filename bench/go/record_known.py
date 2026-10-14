#!/usr/bin/env python3
"""Writes bench/go/known.json from a results file.

    bench/go/record_known.py out/bench/results.json

Every cell of the Go harness that is not a plain correct answer (differs, rejects, unjudged,
error, or an accepted variant other than the plain one) is listed with a reason. The reason is
chosen from what the cell's own detail says, so it describes what happened; a cell whose detail
matches no known pattern stops the script, so a new kind of failure is read by a person before
it is listed. The run itself fails when a listed cell no longer happens or an unlisted one does.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

NODE_TEXT = ("The library writes the document back with different bytes from the input: the first "
             "difference is in the cell's detail.")
EDIT_TEXT = ("The edited document is not the expected bytes: the library rewrites text other than the "
             "edited value, or writes the value in another style (the cell's detail has the first difference).")


def reason(c: dict) -> str | None:
    s, task, v = c["status"], c["task"], c.get("variant") or "spec"
    d = c.get("detail") or ""
    lib = c["library"]
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
        return "The library refused an input that YAML 1.2 accepts: " + d if d else None
    if s == "differs":
        if d.startswith("does not write the input back") or task == "unchanged-emit":
            return NODE_TEXT
        if task in ("edit-script", "patch") and d.startswith("byte "):
            return EDIT_TEXT
        if task == "patch" and d.startswith("value after the patch"):
            return ("The patched value differs: the library goes through JSON, so a float with a whole "
                    "value comes back as an integer.")
        if re.search(r"got -?\d+, expected -?\d+(\.\d+)? \(float\)", d) and task == "dump":
            return ("A float with a whole value is written without a decimal point, so it reads back as an "
                    "integer.")
        if "(float), expected" in d:
            return ("An integer comes back as a float: the library converts to JSON and reads numbers as "
                    "floats.")
        if "missing /on" in d or "unexpected /jobs" in d:
            return "The library reads the YAML 1.1 key `on` as the boolean true, not as the string \"on\"."
        if re.search(r"got true, expected \"", d):
            return "The library reads the YAML 1.1 scalar `y` as the boolean true."
        if "expected a sequence of" in d and "got a mapping" in d:
            return "The library reads only the first document of a stream."
        if task in ("generic-load", "dump") and d.startswith("at /"):
            return "The value differs from the expected one at the pointer in the cell's detail."
        if task == "dump" and d.startswith("read back"):
            return "Read back, the dumped text differs from the expected value at the pointer in the cell's detail."
        if task == "typed-load" and d.startswith("/on/"):
            return "The library reads the YAML 1.1 key `on` as the boolean true, so the model's `on` field stays empty."
        if task == "typed-load":
            return "The decoded model differs from the expected value (see the cell's detail)."
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
            unknown.append(f"{c['library']}/{c['task']}/{c['workload']}: {c['status']} {c.get('detail', '')[:120]}")
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
