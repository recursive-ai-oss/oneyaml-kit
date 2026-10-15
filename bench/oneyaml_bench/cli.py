"""bin/bench: run the benchmarks and generate the report."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import freeze as freeze_mod
from . import readme, report, results, run, shards, workloads

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


def text_hits(text: str) -> list[str]:
    """The kit's public-text check over a string."""
    sys.path.insert(0, str(KIT_ROOT / "tools"))
    from oneyaml_kit import public_text  # noqa: E402

    return [f"{n}: {m.group(0)!r}" for n, line in enumerate(text.split("\n"), 1)
            if (m := public_text.PATTERN.search(line))]


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


def unavailable_libraries(h: dict, env=None) -> dict[str, str]:
    """The libraries of a harness that are compiled in only when an environment variable names
    their checkout, and that are not in this build because it is not set: library id -> variable.

    `optional_libraries` in harness.json maps each variable to the libraries it brings in. Like an
    adapter whose `requires_env` is missing, such a library is not available here, which is not a
    failure: its known deviations cannot be checked, and are not reported as stale."""
    env = os.environ if env is None else env
    return {lib: var for var, libs in h.get("optional_libraries", {}).items() if not env.get(var)
            for lib in libs}


def known_unchecked(result: dict, env=None) -> dict[str, list[str]]:
    """Known-deviation entries left unchecked because their library is not in this build, by
    library, with the variable that would bring it in."""
    out: dict[str, list[str]] = {}
    for lang, h in run.discover().items():
        if lang not in result["harnesses"]:
            continue
        gone = unavailable_libraries(h, env)
        path = h["_dir"] / "known.json"
        known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"entries": []}
        for e in known.get("entries", []):
            lib = e["cell"].split("/", 1)[0]
            if lib in gone:
                out.setdefault(f"{lib} ({gone[lib]} is not set)", []).append(e["cell"])
    return out


def known_problems(result: dict, env=None) -> list[str]:
    """Checks every language's deviation list against the cells measured and against the
    full set of libraries, tasks and workloads. A library that is not in this build (see
    unavailable_libraries) has no cells, so its entries are checked for form only."""
    problems = []
    universe = workloads.universe()
    for lang, h in run.discover().items():
        if lang not in result["harnesses"]:
            continue
        path = h["_dir"] / "known.json"
        known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"entries": []}
        gone = unavailable_libraries(h, env)
        libs = {x["id"] for x in result["harnesses"][lang]["libraries"] if x["role"] != "control"}
        for lib, var in sorted(gone.items()):
            if lib in libs:
                problems.append(f"{lang}: library {lib} is in the build but {var} is not set")
        # The entries of an absent library still get the structural checks (a repeated entry, a
        # missing reason, an unknown workload or task); only the comparison with measured cells
        # cannot happen, because there are none.
        libs |= set(gone)
        problems += [f"{lang}: {p}" for p in results.verify_known(result["cells"], known, lang, universe, libs)]
    return problems


def known_counts(result: dict, problems: list[str]) -> dict[str, int]:
    """How the measured cells stand against the lists of known deviations: `listed` cells are
    deviations that a list names with the status they have, `unlisted` ones are deviations no list
    names, `stale` entries name a deviation that no longer happens (or a cell that does not exist),
    and `other` problems are the rest (an entry with the wrong status, a repeated entry)."""
    entries = {}
    for lang, known in known_lists(result).items():
        for e in known.get("entries", []):
            entries[e["cell"]] = e
    listed = 0
    for c in result["cells"]:
        dev = results.deviation_of(c)
        e = entries.get(results.cell_name(c))
        if dev and e and (e["status"], e.get("variant", "")) == dev:
            listed += 1
    unlisted = sum(p.split(": ", 1)[-1].startswith("unlisted deviation") for p in problems)
    stale = sum(p.split(": ", 1)[-1].startswith("stale known deviation") for p in problems)
    return {"listed": listed, "unlisted": unlisted, "stale": stale, "other": len(problems) - unlisted - stale}


def parse_shard(text: str) -> tuple[int, int]:
    """`I/N` -> (I, N), 1-based."""
    try:
        i, n = (int(x) for x in text.split("/"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not I/N, for example 2/4") from None
    if not 1 <= i <= n:
        raise argparse.ArgumentTypeError(f"shard {i} of {n}: the index must be between 1 and the count")
    return i, n


def cmd_run(a: argparse.Namespace) -> int:
    out = Path(a.out) if a.out else default_out()
    t0 = time.monotonic()
    try:
        result = run.run(a.mode, out, a.lang or None, a.library or [], a.task or [], a.workload or [],
                         a.pin, a.counters, memory_processes=a.memory_processes, shard=a.shard)
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
    k = known_counts(result, problems)
    print(f"known deviations: {k['listed']} listed, {k['unlisted']} unlisted, {k['stale']} stale, "
          f"{k['other']} other problems: {'ok' if not problems else 'MISMATCH'}")
    for what, cells_ in sorted(known_unchecked(result).items()):
        print(f"  not checked: {len(cells_)} known deviations of {what}")
    for p in problems[:5]:
        print(f"  {p}")
    if len(problems) > 5:
        print(f"  ... and {len(problems) - 5} more")
    print(f"results: {out / 'results.json'}\nreport:  {out / 'report.md'}")
    if hits:
        print("public-text check failed on generated output:\n  " + "\n  ".join(hits), file=sys.stderr)
        return 1
    if problems:
        print("known deviations do not match this run:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    return 0


def cmd_plan(a: argparse.Namespace) -> int:
    harnesses = run.discover()
    langs = a.lang or sorted(harnesses)
    unknown = [l for l in langs if l not in harnesses]
    if unknown:
        print(f"bench: no harness for {unknown[0]!r}; have {sorted(harnesses)}", file=sys.stderr)
        return 2
    try:
        plan = shards.plan_for(harnesses, langs, a.mode, a.library or [], a.task or [], a.workload or [], a.shards)
    except shards.PlanError as e:
        print(f"bench: {e}", file=sys.stderr)
        return 2
    print(json.dumps(shards.describe(plan), indent=1))
    return 0


def cmd_merge(a: argparse.Namespace) -> int:
    parts = []
    for d in a.shard_dirs:
        path = Path(d)
        path = path / "results.json" if path.is_dir() else path
        try:
            parts.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            print(f"bench: cannot read {path}: {e}", file=sys.stderr)
            return 1
    try:
        result = shards.merge(parts, allow_incomplete=a.allow_incomplete)
    except shards.MergeError as e:
        print("bench: refusing to merge:\n  " + "\n  ".join(e.problems), file=sys.stderr)
        return 1
    out = Path(a.out)
    run.write(result, out / "results.json")
    files = write_report(result, out)
    hits = public_text_hits(files)
    pub = result["run"]["publishability"]
    print(f"merged {len(parts)} shards: {len(result['cells'])} cells; label: {pub['label']}")
    for r in pub["reasons"]:
        print(f"  not publishable: {r}")
    print(f"results: {out / 'results.json'}\nreport:  {out / 'report.md'}")
    if hits:
        print("public-text check failed on generated output:\n  " + "\n  ".join(hits), file=sys.stderr)
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


def find_first(candidates: list[Path]) -> Path | None:
    return next((p for p in candidates if p.is_file()), None)


def cmd_readme(a: argparse.Namespace) -> int:
    version = (KIT_ROOT / "suites" / "VERSION").read_text(encoding="utf-8").strip()
    res = Path(a.results)
    try:
        bench = Path(a.bench) if a.bench else find_first([res / "bench" / "results.json", res / "results.json"])
        if bench is None:
            raise readme.ReadmeError(f"no benchmark result found in {res} (looked for bench/results.json and results.json)")
        conf_path = Path(a.conformance) if a.conformance else find_first(
            [res / d / f"{a.library}.json" for d in ("conformance", "results", ".")])
        conf = json.loads(conf_path.read_text(encoding="utf-8")) if conf_path else None
        docs = Path(a.docs)
        readme_path = Path(a.readme)
        ctx = readme.Ctx(json.loads(bench.read_text(encoding="utf-8")), a.library, version, conf,
                         readme_path.parent / a.charts.rstrip("/"), a.allow_smoke)
        targets = [readme_path, docs / "performance.md", docs / "conformance.md"]
        new = readme.update(ctx, targets, a.charts.rstrip("/"), docs / "conformance.md")
        changed = readme.check_and_write(ctx, new, ctx.chart_dir, text_hits)
    except (readme.ReadmeError, results.ResultError, OSError, KeyError, ValueError) as e:
        print(f"bench readme: {e}", file=sys.stderr)
        return 1
    print("\n".join(f"updated {p}" for p in changed) or "already up to date")
    return 0


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
    r.add_argument("--memory-processes", type=int, metavar="N",
                   help="processes that measure memory for a harness whose figure differs between processes "
                        "(default: what its harness.json declares for the mode)")
    r.add_argument("--shard", type=parse_shard, metavar="I/N",
                   help="measure only shard I of N of the comparison rows (see `bench plan`); "
                        "combine the shards with `bench merge`")
    r.set_defaults(fn=cmd_run)
    pl = sub.add_parser("plan", help="show how a run is split into shards, as JSON")
    pl.add_argument("--shards", type=int, required=True, metavar="N")
    pl.add_argument("--mode", choices=("quick", "full"), default="full")
    pl.add_argument("--lang", action="append")
    pl.add_argument("--library", action="append")
    pl.add_argument("--task", action="append", choices=report.TASK_ORDER)
    pl.add_argument("--workload", action="append")
    pl.set_defaults(fn=cmd_plan)
    m = sub.add_parser("merge", help="combine the results of the shards of one plan into one result and report")
    m.add_argument("--out", required=True)
    m.add_argument("--allow-incomplete", action="store_true",
                   help="merge although shards are missing; the result is then never publishable")
    m.add_argument("shard_dirs", nargs="+", metavar="SHARD_DIR")
    m.set_defaults(fn=cmd_merge)
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
    m = sub.add_parser("readme", help="write the generated blocks of a library README and docs between their markers")
    m.add_argument("--results", required=True, help="directory with the benchmark result and the conformance results")
    m.add_argument("--library", required=True, help="the library to feature")
    m.add_argument("--readme", required=True)
    m.add_argument("--docs", required=True, help="directory holding performance.md and conformance.md")
    m.add_argument("--charts", default="docs/img", help="chart directory, relative to the README")
    m.add_argument("--bench", help="benchmark result file, when not at <results>/bench/results.json")
    m.add_argument("--conformance", help="conformance result file, when not at <results>/<dir>/<library>.json")
    m.add_argument("--allow-smoke", action="store_true",
                   help="accept a result that is not publishable (the blocks are labelled as such)")
    m.set_defaults(fn=cmd_readme)
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
