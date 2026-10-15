"""The gate: the hand-over tier by default, everything with --full.

* Hand-over (default): the kit core, plus only the areas a change touched (by the files that
  differ from the base branch), and in each area only the quick steps: formatting, lint, the
  unit and self-tests, and a tiny smoke run of a benchmark harness. A change to the kit's own
  code or cases also runs the reference adapter, because those can change what every adapter
  scores. The run fails when it takes longer than its time budget, so the tier cannot grow
  slow unnoticed; a step that is too slow for it belongs in the full tier.
* Full (`--full`): every area, every adapter and every step, including the slow ones (the
  end-to-end benchmark pipeline tests, the quick benchmark run over every workload and the
  counter check). CI runs this.

An area is a part of the repository with its own checks: `core`, one per adapter language
(`rust`, `go`, `csharp`, ...) and the benchmark areas: `bench` (the driver, report and
workloads, no toolchain needed) and one per harness language (`bench-rust`, `bench-go`,
`bench-csharp`), so a change to one harness never builds the others. Areas run side by side
with `--jobs` (by default a few at a time), and their steps one after the other. Every step
runs even after a failure, because the checks are independent and one run should show
everything that is wrong. Each step's output goes to a log under the output directory
and is shown only when the step fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .cases import ROOT

# Shared directories that belong to a language without being an adapter themselves.
SHARED = {"gokit": "go", "dotnet-common": "csharp", "Directory.Build.props": "csharp"}
# Paths of the kit itself. A change here can move any adapter's results.
CORE_CODE = ("tools/", "suites/", "bin/kit", "bin/gate", "tests/support.py", "tests/fixtures/")
# The benchmark harness imports the kit's package (the public-text check), so kit code also
# selects it.
BENCH_DEPENDS_ON = ("tools/",)
# The adapter test module of each language.
LANG_TESTS = {"rust": "test_adapter_oneyaml_rust.py", "go": "test_adapters_go.py",
              "csharp": "test_adapter_dotnet.py"}
# Checks every project given (the .NET SDK is $0) at the same time and fails if any of them does.
DOTNET_FORMAT_ALL = (
    'pids=; for p in "$@"; do "$0" format whitespace "$p" --verify-no-changes & pids="$pids $!"; done; '
    'rc=0; for pid in $pids; do wait "$pid" || rc=1; done; exit $rc')
GO_CHECK = ["sh", "-c", 'test -z "$(gofmt -l .)" && go vet ./... && go test -short ./...']
REFERENCE = "rust"  # the quick adapter that stands in for the rest on a core change
# The benchmark harness languages. Each has an area of its own, `bench-<language>`.
BENCH_LANGS = ("rust", "go", "csharp")
BENCH_REFERENCE = "rust"  # the harness that stands in for the rest on a driver change
# Test modules of the benchmark harness that build and run a real harness end to end. They are
# full-tier work; the rest of bench/tests is quick enough for the hand-over tier. Each entry is a
# fragment of a file name, and each must match a file, so a renamed module cannot slip through.
BENCH_SLOW_TESTS = ("pipeline", "csharp")
# The module that holds each harness's end-to-end tests; the full tier runs them in that
# harness's area.
BENCH_PIPELINE_TESTS = {"rust": "test_pipeline.py", "go": "test_pipeline_go.py", "csharp": "test_csharp.py"}
# The smoke run: a few small workloads, one of them typed, through one harness.
SMOKE_WORKLOADS = ("reference-a", "reference-b", "gha-workflow")
# The hand-over tier's wall-clock budget in seconds. The hard limit is three minutes; the margin
# covers a slower machine and cold caches.
BUDGET_SECONDS = 150
DEFAULT_JOBS = 4
AREA_NAMES_BENCH = ["bench", *(f"bench-{lang}" for lang in BENCH_LANGS)]


@dataclass
class Step:
    area: str
    name: str
    argv: list[str]
    cwd: str = "."
    env: dict = field(default_factory=dict)
    drop_env: tuple = ()
    # "both": every run. "hand-over": only the hand-over tier (a quick stand-in for a slow full
    # step). "full": only --full.
    tier: str = "both"


def py() -> str:
    return sys.executable


def adapter_dirs(root: Path) -> dict[str, dict]:
    """name -> {"language", "dir"} for every adapter, from its manifest."""
    out = {}
    base = root / "adapters"
    if base.is_dir():
        for d in sorted(base.iterdir()):
            m = d / "adapter.json"
            if m.is_file():
                try:
                    lang = json.loads(m.read_bytes()).get("language", "other")
                except ValueError:
                    lang = "other"
                out[d.name] = {"language": lang, "dir": d}
    return out


def bench_area(parts: list[str]) -> str:
    """The benchmark area of a path under bench/: a harness language's own folder or test
    module selects that harness alone; anything else belongs to the shared driver."""
    if len(parts) > 2 and parts[1] in BENCH_LANGS:
        return f"bench-{parts[1]}"
    if len(parts) == 3 and parts[1] == "tests":
        name = parts[2]
        for lang in BENCH_LANGS:
            if lang != BENCH_REFERENCE and lang in name:
                return f"bench-{lang}"
        if "pipeline" in name:
            return f"bench-{BENCH_REFERENCE}"
    return "bench"


def classify(path: str, adapters: dict) -> str | tuple[str, str]:
    """The area of a changed file; for an adapter file, (language, adapter name)."""
    parts = path.split("/")
    if parts[0] == "bench" or path == "bin/bench":
        return bench_area(parts)
    if parts[0] == "adapters" and len(parts) > 1:
        if parts[1] in SHARED:
            return SHARED[parts[1]]
        if parts[1] in adapters:
            return (adapters[parts[1]]["language"], parts[1])
        return "core"
    for lang, test in LANG_TESTS.items():
        if path == f"tests/{test}":
            return lang
    return "core"


def _git(root: Path, *a: str):
    return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True)


def changed_files(root: Path, base: str) -> list[str] | None:
    """Files that differ from the merge base with `base` (committed, uncommitted and new).
    Renames are reported as a deletion and an addition, so the area a file left is selected
    as well as the one it arrived in. None when git or the base is not available."""
    try:
        mb = _git(root, "merge-base", "HEAD", base)
        if mb.returncode != 0:
            return None
        diff = _git(root, "diff", "--name-only", "--no-renames", mb.stdout.strip())
        new = _git(root, "ls-files", "--others", "--exclude-standard")
    except OSError:
        return None
    if diff.returncode != 0 or new.returncode != 0:
        return None
    return sorted({*diff.stdout.split("\n"), *new.stdout.split("\n")} - {""})


def revision(root: Path, base: str) -> dict:
    """What was checked, so a reader can tell whether two runs saw the same tree: HEAD, the
    base and merge base, a fingerprint of the change against the merge base (the binary diff,
    plus the path and content hash of every untracked file) and whether the tree is dirty."""
    try:
        head = _git(root, "rev-parse", "HEAD")
        if head.returncode != 0:
            return {"available": False}
        mb = _git(root, "merge-base", "HEAD", base)
        since = mb.stdout.strip() if mb.returncode == 0 else None
        h = hashlib.sha256()
        if since:
            h.update(_git_bytes(root, "diff", "--binary", "--no-renames", since))
        untracked = sorted(x for x in _git(root, "ls-files", "--others", "--exclude-standard").stdout.split("\n") if x)
        for name in untracked:
            try:
                digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
            except OSError:
                digest = "unreadable"
            h.update(f"untracked {name} {digest}\n".encode())
        dirty = bool(_git(root, "status", "--porcelain").stdout.strip())
        return {"available": True, "head": head.stdout.strip(), "base": base, "merge_base": since,
                "fingerprint": h.hexdigest() if since else None, "untracked_files": len(untracked),
                "dirty": dirty}
    except OSError:
        return {"available": False}


def _git_bytes(root: Path, *a: str) -> bytes:
    return subprocess.run(["git", *a], cwd=root, capture_output=True).stdout


def select_areas(files: list[str], root: Path, full: bool) -> tuple[list[str], dict[str, set[str] | None], str]:
    """(areas to run in order, adapters touched per language or None for all, why)."""
    ads = adapter_dirs(root)
    langs = sorted({a["language"] for a in ads.values()})
    if full:
        return ["core", *langs, *AREA_NAMES_BENCH], {lang: None for lang in langs}, "full mode"
    areas = {"core"}
    touched: dict[str, set[str] | None] = {}
    core_code = False
    bench = False
    driver = False
    for f in files:
        c = classify(f, ads)
        if isinstance(c, tuple):
            lang, name = c
            areas.add(lang)
            if touched.get(lang, set()) is not None:
                touched.setdefault(lang, set()).add(name)
        elif c in langs or c in AREA_NAMES_BENCH:
            areas.add(c)
            if c in langs:
                touched[c] = None  # a shared file or the language's test: every adapter
            # A change to the shared driver can break any harness, so one stands in for them;
            # the driver's own tests need no toolchain.
            if c == "bench" and not f.startswith("bench/tests/"):
                driver = True
        else:
            if f.startswith(CORE_CODE):
                core_code = True
            if f.startswith(BENCH_DEPENDS_ON):
                bench = True
    why = f"{len(files)} changed file(s)"
    if core_code and REFERENCE in langs:
        areas.add(REFERENCE)
        touched[REFERENCE] = None
        why += f"; kit code or cases changed, so the reference adapter ({REFERENCE}) runs too"
    # The oneyaml-csharp adapter shares its linked-build mechanics with the C# harness, and the harness's
    # tests build both, so a change to that adapter runs them.
    if "oneyaml-csharp" in (touched.get("csharp") or ()):
        areas.add("bench-csharp")
        why += "; the oneyaml-csharp adapter changed, so the C# harness's linked builds run"
    if driver and BENCH_REFERENCE in BENCH_LANGS:
        areas.add(f"bench-{BENCH_REFERENCE}")
        why += f"; the benchmark driver changed, so the {BENCH_REFERENCE} harness runs a smoke test"
    if bench:
        areas.add("bench")
        why += "; kit code changed, and the benchmark harness imports it"
    order = ["core", *langs, *AREA_NAMES_BENCH]
    return [a for a in order if a in areas], touched, why


def bench_test_modules(root: Path) -> list[str]:
    """The benchmark test modules the hand-over tier runs: all but the slow end-to-end ones."""
    names = sorted(p.stem for p in (root / "bench" / "tests").glob("test_*.py"))
    return [n for n in names if not any(frag in n for frag in BENCH_SLOW_TESTS)]


def bench_steps(root: Path, out: Path, target: str) -> list[Step]:
    """The benchmark driver, report and workloads. The hand-over tier runs their unit tests,
    which need no compiler but do need node and npm, in both tiers: the independent reader of the
    real files installs its one pinned package with `npm ci` on first use, which needs the network
    once. The full tier then runs a quick run of every harness over every workload and checks
    the instruction counters against their baseline; the
    end-to-end tests of each harness are in its own area."""
    own = {"CARGO_TARGET_DIR": f"{target}-bench"}
    return [
        Step("bench", "benchmark unit tests", [py(), "-m", "unittest", "-q", *bench_test_modules(root)],
             "bench/tests"),
        Step("bench", "benchmark quick run", [py(), "bin/bench", "run"], env=own, tier="full"),
        Step("bench", "benchmark counters",
             [py(), "bin/bench", "check", str(out / "bench" / "results.json"),
              "--baseline", "bench/rust/counters.json"], tier="full"),
    ]


def bench_lang_steps(area: str, root: Path, out: Path, target: str, dotnet: str) -> list[Step]:
    """One benchmark harness. Each language builds into a directory of its own, so areas that
    run side by side never wait on one another's build lock. The hand-over tier checks format
    and lint, runs the harness's own tests and a smoke run over a few small workloads; the
    full tier adds the end-to-end pipeline tests (and, for Rust, the lint of the build without
    the control libraries)."""
    lang = area[len("bench-"):]
    own = {"CARGO_TARGET_DIR": f"{target}-{area}"}
    d = f"bench/{lang}"
    smoke = Step(area, f"benchmark {lang} smoke run",
                 [py(), "bin/bench", "run", "--lang", lang, *[x for w in SMOKE_WORKLOADS for x in ("--workload", w)],
                  "--out", str(out / "bench-smoke" / lang)], env=own,
                 tier="hand-over")  # the full tier's quick run covers every workload
    pipeline = BENCH_PIPELINE_TESTS[lang]
    pipe = Step(area, f"benchmark {lang} pipeline tests",
                [py(), "-m", "unittest", "discover", "-s", "bench/tests", "-p", pipeline, "-q"], env=own,
                tier="full")
    if lang == "rust":
        sel = f"{target}-selftest"
        return [
            Step(area, "benchmark fmt", ["cargo", "fmt", "--check"], d),
            # The self-test build is a superset of the plain build, so its lint covers the hand-over
            # tier; the full tier lints the plain build as well.
            Step(area, "benchmark clippy", ["cargo", "clippy", "--release", "--locked", "--quiet", "--all-targets",
                                            "--", "-D", "warnings"], d, own, tier="full"),
            # The self-test build has fake libraries in it, so it gets its own build directory.
            Step(area, "benchmark self-test clippy",
                 ["cargo", "clippy", "--release", "--locked", "--quiet", "--all-targets", "--features", "selftest",
                  "--", "-D", "warnings"], d, {"CARGO_TARGET_DIR": sel}),
            Step(area, "benchmark self-test", ["cargo", "test", "--release", "--locked", "--quiet", "--features",
                                               "selftest"], d, {"CARGO_TARGET_DIR": sel}),
            smoke, pipe,
        ]
    if lang == "go":
        return [
            Step(area, "benchmark go: gofmt, vet, test", GO_CHECK, d),
            # The self-test build has fake libraries in it, so only its tests use the tag.
            Step(area, "benchmark go self-test",
                 ["sh", "-c", "go vet -tags selftest ./... && go test -short -tags selftest ./..."], d),
            smoke, pipe,
        ]
    return [
        Step(area, "benchmark .NET harness format",
             [dotnet, "format", "whitespace", "bench/csharp/Bench.csproj", "--verify-no-changes"]),
        # The harness's files are checked without an SDK; the class that builds it is the pipeline test.
        Step(area, "benchmark .NET harness files",
             [py(), "-m", "unittest", "-q", "test_csharp.Files"], "bench/tests", tier="hand-over"),
        # The linked builds (the harness and the oneyaml-csharp adapter against an empty stand-in
        # library) take seconds; a project change that breaks them is found here, not in CI.
        Step(area, "benchmark .NET linked builds",
             [py(), "-m", "unittest", "-q", "test_csharp.LinkedBuildStandIns"], "bench/tests", tier="hand-over"),
        smoke, pipe,
    ]


def build_steps(areas: list[str], touched: dict, root: Path, out: Path, full: bool = True) -> list[Step]:
    """The steps of the areas, in order. `full` is every step; without it, the hand-over tier."""
    steps = [s for s in _all_steps(areas, touched, root, out)
             if s.tier == "both" or s.tier == ("full" if full else "hand-over")]
    if full:
        # The slow cases (large generated inputs) run only here: the hand-over tier would spend
        # its budget on them. Every step that runs adapters over the cases asks for them through
        # the environment, which both `kit fast` and the adapter test modules read.
        for s in steps:
            if s.area != "core" and not s.area.startswith("bench"):
                s.env = {**s.env, "KIT_SLOW": "1"}
    return steps


def _all_steps(areas: list[str], touched: dict, root: Path, out: Path) -> list[Step]:
    ads = adapter_dirs(root)
    target = os.environ.get("CARGO_TARGET_DIR") or str(out / "target")
    steps: list[Step] = []
    dotnet = os.environ.get("DOTNET", "dotnet")
    for area in areas:
        if area == "core":
            steps += [
                Step("core", "corpus pins", [py(), "bin/kit", "pins"]),
                # The adapter modules run their static checks here and skip building adapters.
                Step("core", "kit tests", [py(), "-m", "unittest", "discover", "-s", "tests", "-q"],
                     drop_env=("KIT_ADAPTER_TESTS",)),
                Step("core", "public text", [py(), "bin/kit", "public-text"]),
                Step("core", "no executables, dumps or large blobs", [py(), "bin/kit", "blobs"]),
            ]
        elif area == "bench":
            steps += bench_steps(root, out, target)
        elif area.startswith("bench-"):
            steps += bench_lang_steps(area, root, out, target, dotnet)
        else:
            names = sorted(n for n, a in ads.items() if a["language"] == area)
            only = touched.get(area)
            sel_names = names if only is None else sorted(set(names) & only)
            test = LANG_TESTS.get(area)
            if test:
                steps.append(Step(area, f"{area} adapters: kit run and checks",
                                  [py(), "-m", "unittest", "discover", "-s", "tests", "-p", test, "-q"],
                                  env={"KIT_ADAPTER_TESTS": "1"}))
            else:
                for n in sel_names:
                    steps.append(Step(area, f"{n}: kit run", [py(), "bin/kit", "fast", "--adapter", n,
                                                              "--out", str(out / "gate" / n)]))
            projects: list[str] = []
            for n in sel_names:
                d = f"adapters/{n}"
                if area == "rust":
                    steps += [Step(area, f"{n}: fmt", ["cargo", "fmt", "--check"], d),
                              Step(area, f"{n}: clippy", ["cargo", "clippy", "--release", "--locked",
                                                          "--quiet", "--", "-D", "warnings"], d)]
                elif area == "go":
                    steps.append(Step(area, f"{n}: gofmt, vet, test", GO_CHECK, d))
                elif area == "csharp":
                    projects += [f"{d}/{proj.name}" for proj in sorted((ads[n]["dir"]).glob("*.csproj"))]
            if projects:
                # One step for all projects, checked side by side: each check is mostly the start-up
                # of the SDK, so five in a row cost five times what the slowest does.
                steps.append(Step(area, "dotnet format", ["sh", "-c", DOTNET_FORMAT_ALL, dotnet, *projects]))
            # Shared Go code has its own tests and is not an adapter.
            if area == "go" and only is None and (root / "adapters" / "gokit" / "go.mod").is_file():
                steps.append(Step(area, "gokit: gofmt, vet, test", GO_CHECK, "adapters/gokit"))
    return steps


def run_step(step: Step, root: Path, out: Path, base_env: dict, index: int) -> dict:
    env = dict(base_env)
    for k in step.drop_env:
        env.pop(k, None)
    env.update(step.env)
    env.setdefault("CARGO_TARGET_DIR", str(out / "target"))
    env.setdefault("KIT_GO_BIN", str(out / "gobin"))
    env.setdefault("KIT_DOTNET_ARTIFACTS", str(out / "artifacts"))
    log = out / "gate" / f"{index:02d}-{step.area}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    try:
        with open(log, "wb") as f:
            p = subprocess.run(step.argv, cwd=root / step.cwd, env=env, stdout=f, stderr=subprocess.STDOUT)
        rc = p.returncode
    except OSError as e:
        log.write_text(f"cannot run {step.argv[0]}: {e}\n")
        rc = 127
    return {"area": step.area, "name": step.name, "rc": rc, "seconds": time.monotonic() - t0,
            "log": log, "argv": step.argv}


def default_jobs() -> int:
    """Areas to run side by side when --jobs is not given: a few, because each area's build can
    use every core and the memory of a shared host is the tighter limit."""
    return max(1, min(DEFAULT_JOBS, os.cpu_count() or 1))


# Areas that build the most go first when they run side by side.
HEAVY = {"csharp": 5, "bench-csharp": 4, "rust": 3, "bench-rust": 3, "go": 2, "bench-go": 1, "bench": 1}


def weight(area: str) -> int:
    return HEAVY.get(area, 0)


def over_budget(total: float, budget: float | None, results: list[dict]) -> str | None:
    """A message when the hand-over run took longer than its budget, naming the slowest steps
    so the one to move to the full tier is clear; None when within budget or unbudgeted."""
    if budget is None or total <= budget:
        return None
    slow = sorted(results, key=lambda r: -r["seconds"])[:3]
    names = ", ".join(f"{r['area']}: {r['name']} ({r['seconds']:.0f} s)" for r in slow)
    return (f"gate: the hand-over tier took {total:.0f} s, over its budget of {budget:g} s; "
            f"the slowest steps were {names}. Make them faster or move them to the full tier.")


def main(args) -> int:
    root = ROOT
    out = Path(args.out or os.environ.get("KIT_OUT") or root / "out")
    (out / "gate").mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    why = ""
    known = ["core", *sorted({a["language"] for a in adapter_dirs(root).values()}), *AREA_NAMES_BENCH]
    if args.areas:
        areas = [a for a in args.areas.split(",") if a]
        unknown = [a for a in areas if a not in known]
        if unknown or not areas:
            print(f"gate: unknown area(s) {unknown or args.areas!r}; the areas are {', '.join(known)}",
                  file=sys.stderr)
            return 2
        if "core" not in areas and not args.without_core:
            areas.insert(0, "core")
        areas = [a for a in known if a in areas]
        touched = {a: None for a in areas}
        why = "areas named on the command line"
    else:
        files_or_none = None if args.full else changed_files(root, args.base)
        if not args.full and files_or_none is None:
            print(f"gate: cannot compare with {args.base!r}; running the full gate", file=sys.stderr)
            args.full = True
        files = files_or_none or []
        areas, touched, why = select_areas(files, root, args.full)
        if args.full:
            why = "full mode"
    steps = build_steps(areas, touched, root, out, full=args.full)
    rev = revision(root, args.base)
    if rev.get("available"):
        print(f"gate: revision HEAD {rev['head'][:12]}, base {rev['base']} at "
              f"{(rev['merge_base'] or 'unknown')[:12]}, fingerprint "
              f"{(rev['fingerprint'] or 'none')[:16]}, {'dirty' if rev['dirty'] else 'clean'}, "
              f"{rev['untracked_files']} untracked file(s)")
    else:
        print("gate: revision unavailable (no git)")
    print(f"gate: {'full' if args.full else 'hand-over'}; {why}")
    print(f"gate: areas {', '.join(areas)}; {len(steps)} steps")
    if args.plan:
        for s in steps:
            print(f"  [{s.area}] {s.name}: {shlex.join(s.argv)}")
        return 0
    env = dict(os.environ)
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    lock = threading.Lock()
    results: list[dict] = []
    t_all = time.monotonic()

    def run_area(area: str) -> None:
        for i, s in enumerate(steps):
            if s.area != area:
                continue
            r = run_step(s, root, out, env, i)
            with lock:
                results.append(r)
                mark = "ok  " if r["rc"] == 0 else "FAIL"
                print(f"  [{mark}] {s.area}: {s.name} ({r['seconds']:.1f} s)", flush=True)
                if r["rc"] != 0:
                    tail = r["log"].read_text(errors="replace").splitlines()[-25:]
                    print("\n".join("      " + t for t in tail), flush=True)

    jobs = args.jobs if args.jobs else default_jobs()
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        # Slowest first, so the last area to start is not the one that takes longest.
        list(ex.map(run_area, sorted(areas, key=lambda a: -weight(a))))
    total = time.monotonic() - t_all
    by_area: dict[str, float] = {}
    for r in results:
        by_area[r["area"]] = by_area.get(r["area"], 0) + r["seconds"]
    failed = [r for r in results if r["rc"] != 0]
    budget = None if args.full else (BUDGET_SECONDS if args.budget is None else args.budget)
    over = over_budget(total, budget, results)
    print("gate: " + ", ".join(f"{a} {t:.1f} s" for a, t in by_area.items()) + f"; wall-clock {total:.1f} s")
    (out / "gate" / "summary.json").write_text(json.dumps({
        "mode": "full" if args.full else "hand-over", "revision": rev, "areas": areas, "wall_seconds": round(total, 1),
        "jobs": jobs, "budget_seconds": budget, "over_budget": over is not None,
        "areas_seconds": {a: round(t, 1) for a, t in by_area.items()},
        "steps": [{"area": r["area"], "name": r["name"], "ok": r["rc"] == 0, "seconds": round(r["seconds"], 1)}
                  for r in results]}, indent=1) + "\n")
    if over:
        print(over, file=sys.stderr)
    if failed:
        print(f"gate: FAILED ({len(failed)} of {len(results)} steps)")
        return 1
    if over:
        return 1
    print("gate: ok")
    return 0
