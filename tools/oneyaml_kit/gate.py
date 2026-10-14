"""The gate: the hand-over tier by default, everything with --full.

* Hand-over (default): the kit core, plus only the areas a change touched (by the files that
  differ from the base branch), so it stays well inside three minutes however many
  adapters are added. A change to the kit's own code or cases also runs the reference
  adapter, because those can change what every adapter scores.
* Full (`--full`): every area and every adapter. CI runs this.

An area is a part of the repository with its own checks: `core`, one per adapter language
(`rust`, `go`, `csharp`, ...) and `bench`. Areas run one after the other, or side by side
with `--jobs`. Every step runs even after a failure, because the checks are independent and one run should
show everything that is wrong. Each step's output goes to a log under the output directory
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
GO_CHECK = ["sh", "-c", 'test -z "$(gofmt -l .)" && go vet ./... && go test -short ./...']
REFERENCE = "rust"  # the quick adapter that stands in for the rest on a core change


@dataclass
class Step:
    area: str
    name: str
    argv: list[str]
    cwd: str = "."
    env: dict = field(default_factory=dict)
    drop_env: tuple = ()


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


def classify(path: str, adapters: dict) -> str | tuple[str, str]:
    """The area of a changed file; for an adapter file, (language, adapter name)."""
    parts = path.split("/")
    if parts[0] == "bench" or path == "bin/bench":
        return "bench"
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
        return ["core", *langs, "bench"], {lang: None for lang in langs}, "full mode"
    areas = {"core"}
    touched: dict[str, set[str] | None] = {}
    core_code = False
    bench = False
    for f in files:
        c = classify(f, ads)
        if isinstance(c, tuple):
            lang, name = c
            areas.add(lang)
            if touched.get(lang, set()) is not None:
                touched.setdefault(lang, set()).add(name)
        elif c in langs or c == "bench":
            areas.add(c)
            if c in langs:
                touched[c] = None  # a shared file or the language's test: every adapter
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
    if bench:
        areas.add("bench")
        why += "; kit code changed, and the benchmark harness imports it"
    order = ["core", *langs, "bench"]
    return [a for a in order if a in areas], touched, why


def build_steps(areas: list[str], touched: dict, root: Path, out: Path) -> list[Step]:
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
            ]
        elif area == "bench":
            sel = f"{target}-selftest"
            steps += [
                Step("bench", "benchmark tests", [py(), "-m", "unittest", "discover", "-s", "bench/tests", "-q"]),
                Step("bench", "benchmark fmt", ["cargo", "fmt", "--check"], "bench/rust"),
                Step("bench", "benchmark clippy", ["cargo", "clippy", "--release", "--locked", "--quiet",
                                                   "--all-targets", "--", "-D", "warnings"], "bench/rust"),
                # The self-test build has fake libraries in it, so it gets its own build directory.
                Step("bench", "benchmark self-test clippy",
                     ["cargo", "clippy", "--release", "--locked", "--quiet", "--all-targets",
                      "--features", "selftest", "--", "-D", "warnings"], "bench/rust", {"CARGO_TARGET_DIR": sel}),
                Step("bench", "benchmark self-test", ["cargo", "test", "--release", "--locked", "--quiet",
                                                      "--features", "selftest"], "bench/rust",
                     {"CARGO_TARGET_DIR": sel}),
                Step("bench", "benchmark .NET harness format",
                     [dotnet, "format", "whitespace", "bench/csharp/Bench.csproj", "--verify-no-changes"]),
                Step("bench", "benchmark quick run", [py(), "bin/bench", "run"]),
                Step("bench", "benchmark counters",
                     [py(), "bin/bench", "check", str(out / "bench" / "results.json"),
                      "--baseline", "bench/rust/counters.json"]),
            ]
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
            for n in sel_names:
                d = f"adapters/{n}"
                if area == "rust":
                    steps += [Step(area, f"{n}: fmt", ["cargo", "fmt", "--check"], d),
                              Step(area, f"{n}: clippy", ["cargo", "clippy", "--release", "--locked",
                                                          "--quiet", "--", "-D", "warnings"], d)]
                elif area == "go":
                    steps.append(Step(area, f"{n}: gofmt, vet, test", GO_CHECK, d))
                elif area == "csharp":
                    for proj in sorted((ads[n]["dir"]).glob("*.csproj")):
                        steps.append(Step(area, f"{n}: format",
                                          [dotnet, "format", "whitespace", f"{d}/{proj.name}",
                                           "--verify-no-changes"]))
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


def main(args) -> int:
    root = ROOT
    out = Path(args.out or os.environ.get("KIT_OUT") or root / "out")
    (out / "gate").mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    why = ""
    known = ["core", *sorted({a["language"] for a in adapter_dirs(root).values()}), "bench"]
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
    steps = build_steps(areas, touched, root, out)
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

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        list(ex.map(run_area, areas))
    total = time.monotonic() - t_all
    by_area: dict[str, float] = {}
    for r in results:
        by_area[r["area"]] = by_area.get(r["area"], 0) + r["seconds"]
    failed = [r for r in results if r["rc"] != 0]
    print("gate: " + ", ".join(f"{a} {t:.1f} s" for a, t in by_area.items()) + f"; wall-clock {total:.1f} s")
    (out / "gate" / "summary.json").write_text(json.dumps({
        "mode": "full" if args.full else "hand-over", "revision": rev, "areas": areas, "wall_seconds": round(total, 1),
        "areas_seconds": {a: round(t, 1) for a, t in by_area.items()},
        "steps": [{"area": r["area"], "name": r["name"], "ok": r["rc"] == 0, "seconds": round(r["seconds"], 1)}
                  for r in results]}, indent=1) + "\n")
    if failed:
        print(f"gate: FAILED ({len(failed)} of {len(results)} steps)")
        return 1
    print("gate: ok")
    return 0
