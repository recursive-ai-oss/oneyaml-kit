#!/usr/bin/env python3
"""Records an adapter's known deviations from a run.

    adapters/gokit/record_deviations.py <adapter> [<adapter> ...]

It runs the whole fast set through the adapter with an empty list, then writes every
verdict that is not a pass (and is not simply a capability the adapter does not declare)
into the adapter's deviations.json. Each entry's reason is the comparison's own account
of what differed, so the list says what the library did, not what anyone expected of it.

Run it after checking the failures by hand, and read the diff of the list: a recorded
list hides nothing only if someone looked at what went into it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.dont_write_bytecode = True

from oneyaml_kit import compare, runner  # noqa: E402
from oneyaml_kit.cases import load_all  # noqa: E402
from oneyaml_kit.public_text import PATTERN  # noqa: E402

LIMIT = 220


def reason(r: dict, src: dict, verdict: str) -> str:
    detail = (src.get("detail") or r.get("detail") or "").replace("\n", " ")
    if verdict == compare.UNSUPPORTED:
        return "Declined by the adapter: " + detail
    if detail.startswith("unexpected error: "):
        if r["op"] == "patch":
            return "The library refused the patch: " + detail[len("unexpected error: "):]
        return "Rejects valid input: " + detail[len("unexpected error: "):]
    if detail.startswith("expected an error"):
        if r["op"] == "patch":
            return "The patch was applied; the case expects it to be refused."
        return "The library accepted this input; the case expects it to be rejected."
    if detail.startswith("position:"):
        return "Error position differs: " + detail[len("position: "):]
    if detail.startswith("category:"):
        return "Error category differs: " + detail[len("category: "):]
    if detail.startswith("events before the error"):
        return "Events before the error differ: " + detail[len("events before the error: "):]
    if detail.startswith("$"):
        return "Value differs at " + detail
    if detail.startswith("refused;"):
        return "The library refused the input; the case allows only its exact result."
    if detail.startswith(("memory growth", "took ", "peak memory", "no answer within")):
        # Which bound a run reaches first depends on the machine, so the reason names neither.
        return "Over a bound of the case: the memory bound or the time bound, whichever this machine reaches first"
    if detail.startswith("first difference"):
        return "Output differs: " + detail
    return detail or "Fails; see the comparison detail."


def entries(res: dict) -> list[dict]:
    out = []
    for r in res["cases"]:
        for exp in runner.EXPECTATIONS:
            src = r if exp == "primary" else r.get("corrected")
            if src is None:
                continue
            for mode in runner.MODES:
                v = src[mode]
                if v == compare.PASS:
                    continue
                if v == compare.UNSUPPORTED and r.get("reason") == "capability":
                    continue
                text = re.sub(r"\s+", " ", reason(r, src, v)).strip()
                if len(text) > LIMIT:
                    text = text[: LIMIT - 3] + "..."
                if PATTERN.search(text):
                    # Quoted data from a case can look like internal text to the public-text
                    # check, so such a reason keeps its first clause and leaves the data out.
                    text = re.split(r"[:]", text, maxsplit=1)[0] + " (the data compared is in the results)."
                out.append({"case": r["id"], "expectation": exp, "mode": mode,
                            "verdict": v, "reason": text})
    return out


def main(names: list[str]) -> int:
    if not names:
        print(__doc__)
        return 2
    cases = load_all(include_slow=False)
    for name in names:
        d = runner.ADAPTERS / name
        path = d / "deviations.json"
        manifest = runner.load_manifest(name)
        # Run against an empty list so nothing already recorded can mask a verdict.
        path.write_text('{"format": "oneyaml-kit-deviations/1", "entries": []}\n')
        res = runner.run_adapter(name, cases, True)
        es = entries(res)
        lines = ",\n".join("  " + json.dumps(e, ensure_ascii=False) for e in es)
        path.write_text('{"format": "oneyaml-kit-deviations/1",\n "entries": [\n' + lines + "\n ]}\n",
                        encoding="utf-8")
        print(f"{name}: {len(es)} entries -> {path.relative_to(ROOT)} ({manifest['name']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
