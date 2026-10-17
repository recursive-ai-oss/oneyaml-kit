"""bin/bench: run the benchmarks and generate the report."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import environment, readme, reasons, report, results, run, workloads

ROOT = Path(__file__).resolve().parents[1]
KIT_ROOT = ROOT.parent


def default_out() -> Path:
    return Path(os.environ.get("KIT_OUT", KIT_ROOT / "out")) / "bench"


def text_hits(text: str) -> list[str]:
    """The kit's public-text check over a string."""
    sys.path.insert(0, str(KIT_ROOT / "tools"))
    from oneyaml_kit import public_text  # noqa: E402

    return [f"{n}: {m.group(0)!r}" for n, line in enumerate(text.split("\n"), 1)
            if (m := public_text.PATTERN.search(line))]


def public_text_hits(paths: list[Path]) -> list[str]:
    return [f"{p.name}:{h}" for p in paths for h in text_hits(p.read_text(encoding="utf-8"))]


def write_report(result: dict, out: Path) -> list[Path]:
    path = out / "report.md"
    path.write_text(report.render(result, out), encoding="utf-8")
    return [path] + sorted((out / "charts").glob("*.svg"))


def report_stale(result: dict) -> bool:
    """Says which curated reasons match nothing in a full run; the report holds them too."""
    old = reasons.stale(result)
    if old:
        print("stale reasons in reasons.json (no cell of this run uses them):\n  "
              + "\n  ".join(reasons.stale_lines(old)), file=sys.stderr)
    return bool(old)


def find_first(paths: list[Path]) -> Path | None:
    return next((p for p in paths if p.is_file()), None)


def cmd_run(a: argparse.Namespace) -> int:
    out = Path(a.out) if a.out else default_out()
    t0 = time.monotonic()
    try:
        result = run.run(a.mode, out, a.lang or None, a.library or [], a.task or [], a.workload or [], a.smoke)
    except run.HarnessError as e:
        print(f"bench: {e}", file=sys.stderr)
        return 2
    run.write(result, out / "results.json")
    hits = public_text_hits(write_report(result, out))
    by: dict[str, int] = {}
    for c in result["cells"]:
        by[c["status"]] = by.get(c["status"], 0) + 1
    print(f"{len(result['cells'])} cells ({', '.join(f'{v} {k}' for k, v in sorted(by.items()))}) "
          f"in {time.monotonic() - t0:.1f} s{'; smoke result' if result['run']['smoke'] else ''}")
    missing = results.missing_languages(result)
    if missing:
        print(f"INCOMPLETE: no results for {', '.join(missing)}")
    print(f"results: {out / 'results.json'}\nreport:  {out / 'report.md'}")
    if hits:
        print("public-text check failed on generated output:\n  " + "\n  ".join(hits), file=sys.stderr)
        return 1
    return 1 if report_stale(result) else 0


def cmd_report(a: argparse.Namespace) -> int:
    result = results.load(a.result)
    if result is None:
        print(f"bench report: no results in {a.result}", file=sys.stderr)
        return 1
    out = Path(a.out) if a.out else (Path(a.result) if Path(a.result).is_dir() else Path(a.result).parent)
    hits = public_text_hits(write_report(result, out))
    if hits:
        print("\n".join(hits), file=sys.stderr)
        return 1
    print(f"wrote {out / 'report.md'}")
    return 1 if report_stale(result) else 0


def cmd_workloads(a: argparse.Namespace) -> int:
    out = Path(a.out) if a.out else default_out()
    for m in workloads.prepare(out, ("quick",) if a.tier == "quick" else ("quick", "full")):
        print(f"{m['id']:28} {m['kind']:11} {m['input_bytes']:>9} bytes  {', '.join(m['tasks'])}")
    return 0


def cmd_readme(a: argparse.Namespace) -> int:
    revision = environment.renderer_revision(KIT_ROOT)
    res = Path(a.results)
    try:
        bench = results.load(a.bench) if a.bench else next(
            (r for r in (results.load(d) for d in (res / "bench", res) if d.is_dir()) if r), None)
        if bench is None:
            raise readme.ReadmeError(f"no benchmark result found in {res} (looked for bench/results.json and results.json)")
        conf_path = Path(a.conformance) if a.conformance else find_first(
            [res / d / f"{a.library}.json" for d in ("conformance", "results", ".")])
        conf = json.loads(conf_path.read_text(encoding="utf-8")) if conf_path else None
        docs = Path(a.docs)
        readme_path = Path(a.readme)
        ctx = readme.Ctx(bench, a.library, revision, conf,
                         readme_path.parent / a.charts.rstrip("/"), a.allow_smoke, a.library_source,
                         a.chart_omit_task or ())
        targets = [readme_path, docs / "performance.md", docs / "conformance.md"]
        new = readme.update(ctx, targets, a.charts.rstrip("/"), docs / "conformance.md", a.chart_base)
        changed = readme.check_and_write(ctx, new, ctx.chart_dir, text_hits)
    except (readme.ReadmeError, results.ResultError, OSError, KeyError, ValueError) as e:
        print(f"bench readme: {e}", file=sys.stderr)
        return 1
    print("\n".join(f"updated {p}" for p in changed) or "already up to date")
    return 0


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
    r.add_argument("--smoke", action="store_true", default=None,
                   help="label the run a smoke result; a quick run always is, a full run is a measurement wherever it runs")
    r.set_defaults(fn=cmd_run)
    g = sub.add_parser("report", help="render a report from a results file")
    g.add_argument("result", help="a results file, or the directory of a run (a run cut short has one file per language)")
    g.add_argument("--out")
    g.set_defaults(fn=cmd_report)
    w = sub.add_parser("workloads", help="write the workload set")
    w.add_argument("--out")
    w.add_argument("--tier", choices=("quick", "full"), default="full")
    w.set_defaults(fn=cmd_workloads)
    m = sub.add_parser("readme", help="write generated blocks between markers: bench-chart in the README, bench-hero "
                       "and bench-full on docs/performance.md (every loss appears there; the chart cannot omit a "
                       "losing task), conformance blocks on docs/conformance.md")
    m.add_argument("--results", required=True, help="directory with the benchmark result and the conformance results")
    m.add_argument("--library", required=True, help="the library to feature")
    m.add_argument("--readme", required=True)
    m.add_argument("--docs", required=True, help="directory holding performance.md and conformance.md")
    m.add_argument("--charts", default="docs/img", help="chart directory, relative to the README")
    m.add_argument("--bench", help="benchmark result file, when not at <results>/bench/results.json")
    m.add_argument("--conformance", help="conformance result file, when not at <results>/<dir>/<library>.json")
    m.add_argument("--chart-base", metavar="URL",
                   help="write chart image links as URL/<charts> (absolute) instead of paths relative to the file. "
                        "Pin it to a tag, never a branch (a branch name is refused); without it the links are "
                        "relative, so a tagged README shows the charts of its own version")
    m.add_argument("--library-source", metavar="DIR",
                   help="a checkout of the featured library; its release tag is shown beside the commit when the "
                        "checkout is at that commit and the run did not record the tag")
    m.add_argument("--chart-omit-task", action="append", metavar="TASK",
                   help="leave this task (an id from the result, e.g. unchanged-emit) out of the hero and chart "
                        "blocks, including the chart's alt text and description; repeatable. The full table keeps "
                        "every task. An id the result does not have is an error, and so is a task we lose, mix or have no "
                        "time for: the chart never drops a loss")
    m.add_argument("--allow-smoke", action="store_true",
                   help="accept a smoke result (the blocks are labelled as such)")
    m.set_defaults(fn=cmd_readme)
    a = p.parse_args(argv)
    return a.fn(a)
