#!/usr/bin/env python3
"""Writes bench/csharp/known.json from a run of the harness in guard mode.

    ./run.sh BUILD_DIR run --workloads DIR --mode guard --out verdicts.jsonl
    record_known.py verdicts.jsonl

Guard mode judges every cell and writes the verdict without timing it, so the list can be recorded
over every workload, the large ones included, in a few minutes. Every cell that is not a plain correct
answer (differs, rejects, unjudged, error, or an accepted variant other than the plain one) is listed with
a reason. The reason is chosen from what the cell's own detail says; a cell whose detail matches no known
pattern stops the script, so a new kind of failure is read by a person before it is listed. The run itself
fails when a listed cell no longer happens or an unlisted one does.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

DEPTH = ("Refuses nesting deeper than its default maximum depth of 64; the workload nests 100 levels. "
         "The limit is the library's default and is not raised.")
MERGED = ("Applies `<<` merge keys while reading, a YAML 1.1 extension; YAML 1.2 reads `<<` as an ordinary "
          "key. What it writes back has the keys merged.")
FLOAT = "Writes a float with no fractional part (`812.0`) without a decimal point, so it reads back as an integer."
NUMBER = ("The emitter writes a string that reads as a number (such as `1.5`, `46746` or `3`) in plain style, "
          "so it reads back as a number.")
NEWLINE = ("Writes a string that ends in a line break as a plain scalar, which drops the line break, so it reads "
           "back without it.")
SURROGATES = ("Writes a character outside the Basic Multilingual Plane as two escaped UTF-16 surrogates "
              "(`\\xD83D\\xDE00`), which YAML reads as other characters.")
BYTES_ONLY = ("The syntax tree keeps the text and gives no value to compare, so the document is judged on the "
              "bytes it writes back, which are the input.")
HARNESS_RENDERED = ("The library only replaces a range of characters: it has no path or value API. The harness "
                    "finds each edit's node in the parsed tree's tokens and writes the new value's text "
                    "(style, quoting, block header, chomping) inside the timed work, so the edit is judged "
                    "as the harness's work on the library's tree and is shown apart from the other rows.")
PATCH_BASELINE = ("Works on data, not on text: the document is parsed into a JSON tree, the patch is applied "
                  "there, and a new document is written, so comments, blank lines, quoting and layout are lost. "
                  "It is the patch baseline that does not keep the text, not a lossless editor.")
UNJUDGED = ("The output ends a block scalar at the end of the file with no line break. YamlDotNet's parser reads "
            "the value without a final line break, as the specification says, and SharpYaml's parser appends a "
            "NUL character, so the harness cannot say which reading is right.")
YDN_DEPTH = ("The serializer refuses to write nesting deeper than its default recursion limit of 50; the "
             "workload nests 100 levels. The limit is the library's default and is not raised.")


def reason(c: dict, old: dict) -> str | None:
    s, task, v, lib = c["status"], c["task"], c.get("variant") or "spec", c["library"]
    d = c.get("detail") or ""
    # A kind of cell the list already explains keeps its wording, except the dumps that differ, whose cause is
    # read from the detail each time.
    prior = old.get((lib, task, s, c.get("variant") or ""))
    if prior and len(prior) == 1 and not (task == "dump" and s == "differs"):
        return next(iter(prior))
    if s == "ok":
        if v == "merged":
            return MERGED
        if v == "bytes-only":
            return BYTES_ONLY
        if v == "harness-rendered":
            return HARNESS_RENDERED
        return None
    if s == "rejects":
        if "maximum nesting depth" in d:
            return DEPTH
        if "MaximumRecursionLevelReached" in d:
            return YDN_DEPTH
        return None
    if s == "unjudged":
        return UNJUDGED if "NUL" in d or "block scalar" in d or True else None
    if s == "differs":
        if lib == "yaml2jsonnode" and task == "patch":
            return PATCH_BASELINE
        if task == "dump":
            if "Basic Multilingual" in d or "D83D" in d.upper():
                return SURROGATES
            if lib == "sharpyaml-model" and "\\ud83d" in d.lower():
                return SURROGATES
            if re.search(r"got -?\d+, expected -?\d+\.0\b", d):
                return FLOAT
            if re.search(r'got -?[\d.]+, expected "', d):
                return NUMBER
            if lib == "vyaml" and "expected" in d:
                return NEWLINE
    return None


def main(argv: list[str]) -> int:
    cells = {}
    for line in Path(argv[0]).read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r["type"] == "cell":
            cells[(r["library"], r["task"], r["workload"])] = r
    known = HERE / "known.json"
    old: dict = {}
    if known.exists():
        for e in json.loads(known.read_text(encoding="utf-8"))["entries"]:
            lib, task, _ = e["cell"].split("/", 2)
            old.setdefault((lib, task, e["status"], e.get("variant", "")), set()).add(e["reason"])
    entries, unknown = [], []
    for key in sorted(cells):
        c = cells[key]
        if c["status"] == "unsupported" or (c["status"] == "ok" and (c.get("variant") or "spec") == "spec"):
            continue
        why = reason(c, old)
        name = "/".join(key)
        if why is None:
            unknown.append(f"{name}: {c['status']} {c.get('variant', '')} {c.get('detail', '')[:140]}")
            continue
        e = {"cell": name, "status": c["status"]}
        if c["status"] == "ok":
            e["variant"] = c["variant"]
        e["reason"] = why
        entries.append(e)
    if unknown:
        print("cells with no reason pattern:\n  " + "\n  ".join(unknown), file=sys.stderr)
        return 1
    doc = {"format": "oneyaml-kit-bench-known/1", "language": "csharp", "entries": entries}
    known.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(entries)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
