"""Command line: bin/kit <command>."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import report as report_mod
from .cases import (CaseError, ROOT, current_pins, load_all)
from .public_text import repo_files, scan
from .runner import AdapterUnavailable, RunError, list_adapters, run_adapter

PINS_FILE = ROOT / "tests" / "pins.json"


def out_dir(arg: str | None) -> Path:
    d = Path(arg or os.environ.get("KIT_OUT") or ROOT / "out")
    d.mkdir(parents=True, exist_ok=True)
    return d


def select(cases, args):
    complete = True
    if args.suite != "all":
        want = "yaml-test-suite" if args.suite == "yts" else "own"
        cases = [c for c in cases if c.suite == want]
        complete = False
    if args.category:
        cases = [c for c in cases if c.category in args.category]
        complete = False
    if args.case:
        cases = [c for c in cases if c.id in args.case]
        complete = False
    return cases, complete


def cmd_run(args) -> int:
    try:
        cases, complete = select(load_all(include_slow=args.slow or None), args)
    except CaseError as e:
        print(f"case error: {e}", file=sys.stderr)
        return 2
    names = args.adapter or list_adapters()
    if not names:
        print("no adapters found", file=sys.stderr)
        return 2
    out = out_dir(args.out)
    (out / "results").mkdir(exist_ok=True)
    results, skipped, status = [], [], 0
    t0 = time.monotonic()
    for name in names:
        try:
            res = run_adapter(name, cases, complete, timeout=args.timeout,
                              skip_build=args.no_build, out=out)
        except AdapterUnavailable as e:
            # Not a failure: the machine lacks something the adapter needs. Say so.
            reason = str(e).split(": not available: ", 1)[-1]
            print(f"{name}: SKIPPED, not available: {reason}")
            skipped.append((name, reason))
            continue
        except RunError as e:
            print(f"{name}: {e}", file=sys.stderr)
            return 2
        (out / "results" / f"{name}.json").write_text(
            json.dumps(res, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        results.append(res)
        d = res["deviations"]
        n = len(res["cases"])
        p = {m: sum(1 for r in res["cases"] if r[m] == "pass") for m in ("loose", "strict")}
        print(f"{res['adapter']['name']} {res['adapter']['version']}: {n} cases, "
              f"loose {p['loose']}, strict {p['strict']}, in {res['run']['seconds']} s; "
              f"deviations listed {d['listed']}, unlisted {len(d['unlisted'])}, "
              f"stale {len(d['stale']) + len(d['unknown'])}")
        if not d["ok"]:
            status = 1
            for line in (d["unlisted"] + d["stale"] + d["unknown"])[:20]:
                print("  - " + line)
    md = report_mod.render(results, skipped)
    (out / "conformance.md").write_text(md, encoding="utf-8")
    hits = scan([out / "conformance.md"], root=out)
    if hits:
        print("generated report fails the public-text check:\n" + "\n".join(hits), file=sys.stderr)
        status = 1
    print(f"wrote {out / 'conformance.md'} ({time.monotonic() - t0:.1f} s total)")
    return status


def cmd_report(args) -> int:
    results = [json.loads(Path(p).read_bytes()) for p in args.results]
    md = report_mod.render(results)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
    else:
        sys.stdout.write(md)
    return 0


def cmd_pins(args) -> int:
    cur = current_pins()
    if args.write:
        PINS_FILE.parent.mkdir(exist_ok=True)
        PINS_FILE.write_text(json.dumps(cur, indent=1, sort_keys=True) + "\n")
        print(f"wrote {PINS_FILE}")
        return 0
    pinned = json.loads(PINS_FILE.read_bytes())
    if pinned == cur:
        print("pins match")
        return 0
    print("pins differ; run `bin/kit pins --write` after checking the change", file=sys.stderr)
    return 1


def cmd_public_text(args) -> int:
    paths = [Path(p) for p in args.paths] if args.paths else repo_files()
    hits = scan(paths)
    for h in hits:
        print(h)
    print(f"public-text: {len(paths)} files, {len(hits)} hits")
    return 1 if hits else 0


def cmd_list(args) -> int:
    cases = load_all(include_slow=True)
    from collections import Counter
    c = Counter((x.suite, x.category, x.op, x.expect["kind"]) for x in cases)
    for k, v in sorted(c.items()):
        print(f"{v:5d}  {' / '.join(k)}")
    print(f"{len(cases):5d}  total")
    return 0


def cmd_coverage(args) -> int:
    from . import coverage
    md = coverage.render(load_all(include_slow=True))
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
    else:
        sys.stdout.write(md)
    return 0


def cmd_gate(args) -> int:
    from . import gate
    return gate.main(args)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kit", description="oneyaml-kit conformance runner")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def run_opts(p):
        p.add_argument("--adapter", action="append", help="adapter name (default: all)")
        p.add_argument("--suite", choices=("all", "yts", "own"), default="all")
        p.add_argument("--category", action="append")
        p.add_argument("--case", action="append", help="case id (repeatable)")
        p.add_argument("--out", help="output directory (default: $KIT_OUT or ./out)")
        p.add_argument("--timeout", type=float, help="per-case timeout in seconds")
        p.add_argument("--slow", action="store_true", help="include cases tagged slow (also on when KIT_SLOW is set)")
        p.add_argument("--no-build", action="store_true", help="do not run the adapter build step")

    p = sub.add_parser("fast", help="run the fast mode and write results and the report")
    run_opts(p)
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("run", help="same as fast")
    run_opts(p)
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("report", help="render a report from result files")
    p.add_argument("results", nargs="+")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("pins", help="verify or rewrite the corpus pins")
    p.add_argument("--write", action="store_true")
    p.set_defaults(fn=cmd_pins)
    p = sub.add_parser("public-text", help="scan files for internal process text")
    p.add_argument("paths", nargs="*")
    p.set_defaults(fn=cmd_public_text)
    p = sub.add_parser("list", help="count cases by suite, category and operation")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("coverage", help="which YAML 1.2.2 productions the own cases name (names, not coverage) "
                                        "and which error categories they expect")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_coverage)
    p = sub.add_parser("gate", help="the hand-over gate (areas a change touched) or --full")
    p.add_argument("--full", action="store_true", help="every area and adapter (what CI runs)")
    p.add_argument("--base", default="main", help="branch to compare with (default: main)")
    p.add_argument("--areas", help="comma-separated areas to run instead of choosing by changes")
    p.add_argument("--without-core", action="store_true", help="with --areas: do not add the core area")
    p.add_argument("--jobs", type=int, default=0, help="areas to run side by side (default: up to 4)")
    p.add_argument("--budget", type=float, help="hand-over only: fail when the run takes longer than this many "
                   "seconds (default 150; the limit for the tier is three minutes)")
    p.add_argument("--plan", action="store_true", help="print the steps and stop")
    p.add_argument("--out", help="output directory (default: $KIT_OUT or ./out)")
    p.set_defaults(fn=cmd_gate)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
