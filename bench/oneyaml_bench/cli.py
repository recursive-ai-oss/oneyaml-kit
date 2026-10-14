"""bin/bench: run the benchmarks and generate the report."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import freeze as freeze_mod
from . import report, results, run, workloads

ROOT = Path(__file__).resolve().parents[1]
KIT_ROOT = ROOT.parent


def default_out() -> Path:
    return Path(os.environ.get("KIT_OUT", KIT_ROOT / "out")) / "bench"


def public_text_hits(paths: list[Path]) -> list[str]:
    """The kit's public-text check over generated files."""
    sys.path.insert(0, str(KIT_ROOT / "tools"))
    from oneyaml_kit import public_text  # noqa: E402

    hits = []
    for p in paths:
        for n, line in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):
            m = public_text.PATTERN.search(line)
            if m:
                hits.append(f"{p.name}:{n}: {m.group(0)!r}")
    return hits


def write_report(result: dict, out: Path) -> list[Path]:
    md = report.render(result, out)
    path = out / "report.md"
    path.write_text(md, encoding="utf-8")
    files = [path] + sorted((out / "charts").glob("*.svg"))
    return files


def known_lists(result: dict) -> dict[str, dict]:
    """The parsed known-deviations file of every language in the result."""
    out = {}
    for lang, h in run.discover().items():
        if lang in result["harnesses"]:
            path = h["_dir"] / "known.json"
            out[lang] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"entries": []}
    return out


def known_problems(result: dict) -> list[str]:
    """Checks every language's deviation list against the cells measured and against the
    full set of libraries, tasks and workloads."""
    problems = []
    universe = workloads.universe()
    for lang, h in run.discover().items():
        if lang not in result["harnesses"]:
            continue
        path = h["_dir"] / "known.json"
        known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"entries": []}
        libs = {x["id"] for x in result["harnesses"][lang]["libraries"] if x["role"] != "control"}
        problems += [f"{lang}: {p}" for p in results.verify_known(result["cells"], known, lang, universe, libs)]
    return problems


def cmd_run(a: argparse.Namespace) -> int:
    out = Path(a.out) if a.out else default_out()
    t0 = time.monotonic()
    try:
        result = run.run(a.mode, out, a.lang or None, a.library or [], a.task or [], a.workload or [],
                         a.pin, a.counters)
    except run.HarnessError as e:
        print(f"bench: {e}", file=sys.stderr)
        return 2
    # Check the list of known deviations first: a wrong list must never reach a publishable report.
    problems = known_problems(result)
    results.add_reasons(result, [f"the list of known deviations does not match this run "
                                 f"({len(problems)} problems, the first: {problems[0]})"] if problems else [])
    # The reason of each listed deviation goes into the result, so the report can say why a cell has no number.
    results.attach_known(result, known_lists(result))
    run.write(result, out / "results.json")
    files = write_report(result, out)
    hits = public_text_hits(files)
    cells = result["cells"]
    by = {}
    for c in cells:
        by[c["status"]] = by.get(c["status"], 0) + 1
    pub = result["run"]["publishability"]
    print(f"{len(cells)} cells ({', '.join(f'{v} {k}' for k, v in sorted(by.items()))}) "
          f"in {time.monotonic() - t0:.1f} s; label: {pub['label']}")
    for r in pub["reasons"]:
        print(f"  not publishable: {r}")
    print(f"results: {out / 'results.json'}\nreport:  {out / 'report.md'}")
    if hits:
        print("public-text check failed on generated output:\n  " + "\n  ".join(hits), file=sys.stderr)
        return 1
    if problems:
        print("known deviations do not match this run:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    result = json.loads(Path(a.result).read_text(encoding="utf-8"))
    out = Path(a.out) if a.out else Path(a.result).parent
    files = write_report(result, out)
    hits = public_text_hits(files)
    if hits:
        print("\n".join(hits), file=sys.stderr)
        return 1
    print(f"wrote {out / 'report.md'}")
    return 0


def cmd_workloads(a: argparse.Namespace) -> int:
    out = Path(a.out) if a.out else default_out()
    tiers = ("quick",) if a.tier == "quick" else ("quick", "full")
    metas = workloads.prepare(out, tiers)
    for m in metas:
        print(f"{m['id']:28} {m['kind']:11} {m['input_bytes']:>9} bytes  {', '.join(m['tasks'])}")
    return 0


def cmd_freeze(a: argparse.Namespace) -> int:
    h = run.discover()["rust"]
    build_dir = Path(os.environ.get("CARGO_TARGET_DIR") or default_out() / "target")
    run.build(h, build_dir)
    cmd = run.command(h, build_dir, None)
    return 1 if freeze_mod.freeze(cmd, workloads.REAL_DIR, write=not a.check) else 0


def counter_view(result: dict) -> dict:
    """The deterministic figures of our own libraries, keyed by cell."""
    view = {}
    for c in result["cells"]:
        if c["status"] != "ok" or c["role"] != "own":
            continue
        key = f"{c['language']}/{c['library']}/{c['task']}/{c['workload']}"
        view[key] = {"allocations": c["memory"]["allocations"], "allocated_bytes": c["memory"]["allocated_bytes"],
                     "peak_bytes": c["memory"]["peak_bytes"]}
        if c.get("instructions"):
            view[key]["instructions"] = c["instructions"]
    return view


# Allowed growth of a deterministic figure before the gate fails. Allocation counts repeat
# to within a few percent (the edit path of the larger Helm file varies by up to 2% with the
# hash seed); instruction counts move a little with the toolchain.
COUNTER_TOLERANCE = {"allocations": 0.03, "allocated_bytes": 0.01, "peak_bytes": 0.01, "instructions": 0.02}


def compare_counters(current: dict, baseline: dict) -> list[str]:
    problems = []
    for key, base in baseline.items():
        if key not in current:
            problems.append(f"stale baseline entry: {key} was not measured")
            continue
        for metric, tol in COUNTER_TOLERANCE.items():
            if metric in base and metric in current[key] and current[key][metric] > base[metric] * (1 + tol):
                problems.append(f"{key}: {metric} rose from {base[metric]} to {current[key][metric]}")
    return problems


def cmd_baseline(a: argparse.Namespace) -> int:
    view = counter_view(json.loads(Path(a.result).read_text(encoding="utf-8")))
    Path(a.out).write_text(json.dumps(view, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(view)} cells written to {a.out}")
    return 0


def cmd_check(a: argparse.Namespace) -> int:
    cur = counter_view(json.loads(Path(a.result).read_text(encoding="utf-8")))
    base = json.loads(Path(a.baseline).read_text(encoding="utf-8"))
    problems = compare_counters(cur, base)
    new = sorted(set(cur) - set(base))
    for p in problems:
        print(p)
    if new:
        print(f"{len(new)} measured cells are not in the baseline yet")
    print("counter check:", "FAILED" if problems else "ok")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bench", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="measure, then write results.json, report.md and charts")
    r.add_argument("--mode", choices=("quick", "full"), default="quick")
    r.add_argument("--out")
    r.add_argument("--lang", action="append")
    r.add_argument("--library", action="append")
    r.add_argument("--task", action="append", choices=report.TASK_ORDER)
    r.add_argument("--workload", action="append")
    r.add_argument("--pin", type=int, help="pin the harness to one CPU")
    r.add_argument("--counters", action="store_true", help="also count instructions with callgrind")
    r.set_defaults(fn=cmd_run)
    g = sub.add_parser("report", help="render a report from a results file")
    g.add_argument("result")
    g.add_argument("--out")
    g.set_defaults(fn=cmd_report)
    w = sub.add_parser("workloads", help="write the workload set")
    w.add_argument("--out")
    w.add_argument("--tier", choices=("quick", "full"), default="full")
    w.set_defaults(fn=cmd_workloads)
    f = sub.add_parser("freeze", help="(re)freeze the expected values of the real-world files")
    f.add_argument("--check", action="store_true", help="fail if a frozen file is out of date")
    f.set_defaults(fn=cmd_freeze)
    b = sub.add_parser("baseline", help="write the deterministic counters of a result as a baseline")
    b.add_argument("result")
    b.add_argument("--out", required=True)
    b.set_defaults(fn=cmd_baseline)
    c = sub.add_parser("check", help="fail when the deterministic counters regress against a baseline")
    c.add_argument("result")
    c.add_argument("--baseline", required=True)
    c.set_defaults(fn=cmd_check)
    a = p.parse_args(argv)
    return a.fn(a)
