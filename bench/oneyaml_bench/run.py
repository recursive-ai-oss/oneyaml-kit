"""Builds each language's harness, runs it, and assembles one result file.

A harness is a program with a fixed command line and output (docs/benchmarks.md):

    <command> run --workloads DIR --out FILE --mode quick|full
              [--library ID]... [--task ID]... [--workload ID]... [--skip LIB/TASK/WORKLOAD]...

It writes JSON lines: one `harness` record, then for each cell a `begin` mark followed by a
`cell` record. The mark lets this driver name the cell that killed the process, record it as an
`error` and start the harness again past it.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from . import environment, results, taskstate, workloads

ROOT = Path(__file__).resolve().parents[1]
MAX_RESTARTS = 50


class HarnessError(Exception):
    pass


def say(message: str) -> None:
    """Progress goes out at once: a run under a launcher is watched through a log file."""
    print(message, flush=True)


def discover() -> dict[str, dict]:
    """Every language directory with a harness.json."""
    found = {}
    for p in sorted(ROOT.glob("*/harness.json")):
        h = json.loads(p.read_text(encoding="utf-8"))
        h["_dir"] = p.parent
        found[h["language"]] = h
    return found


def _subst(arg: str, build_dir: Path) -> str:
    return arg.replace("{build}", str(build_dir))


def build(h: dict, build_dir: Path) -> None:
    env = dict(os.environ)
    for k, v in h.get("build_env", {}).items():
        env[k] = _subst(v, build_dir)
    cmd = [_subst(a, build_dir) for a in h["build"]]
    r = subprocess.run(cmd, cwd=h["_dir"], env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise HarnessError(f"building the {h['language']} harness failed:\n{r.stderr[-2000:]}")


def parse_lines(path: Path) -> tuple[dict | None, list[dict], list[str]]:
    """(harness record, cell records, names of begun cells) from a harness output file."""
    harness, cells, begun = None, [], []
    if not path.exists():
        return harness, cells, begun
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # blank, or a line cut short by a crash
        t = rec.get("type")
        if t == "harness":
            harness = harness or rec
        elif t == "begin":
            begun.append(rec["cell"])
        elif t == "cell":
            cells.append(rec)
    return harness, cells, begun


class Progress:
    """Follows the harness output file while the harness runs and prints one line per finished cell."""

    def __init__(self, path: Path, lang: str, log):
        self.path, self.lang, self.log = path, lang, log
        self.pos, self.began, self.n = 0, time.monotonic(), 0

    def poll(self) -> None:
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except FileNotFoundError:
            return
        # Only whole lines: the harness may be part-way through writing one.
        end = data.rfind(b"\n") + 1
        self.pos += end
        for line in data[:end].decode("utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "begin":
                self.began = time.monotonic()
            elif rec.get("type") == "cell":
                self.n += 1
                self.log(f"  {self.lang} {rec['library']} {rec['task']} {rec['workload']}: {rec['status']} "
                         f"({time.monotonic() - self.began:.1f} s)")


def run_harness(h: dict, build_dir: Path, workloads_dir: Path, mode: str, out_dir: Path,
                libraries=(), tasks=(), only=(), log=say) -> tuple[dict, list[dict]]:
    """Runs the harness to completion, restarting past any cell that kills it."""
    lang = h["language"]
    harness, cells, skip = None, [], []
    for attempt in range(MAX_RESTARTS + 1):
        out = out_dir / f"{lang}.jsonl"
        out.unlink(missing_ok=True)
        cmd = [_subst(a, build_dir) for a in h["command"]]
        cmd += ["run", "--workloads", str(workloads_dir), "--out", str(out), "--mode", mode]
        for kind, vals in (("--library", libraries), ("--task", tasks), ("--workload", only), ("--skip", skip)):
            for v in vals:
                cmd += [kind, v]
        # The harness's messages go to a file, not a pipe nobody reads while it runs.
        err_path = out_dir / f"{lang}.stderr"
        progress = Progress(out, lang, log)
        with open(err_path, "wb") as err:
            proc = subprocess.Popen(cmd, cwd=h["_dir"], stdout=subprocess.DEVNULL, stderr=err)
            while proc.poll() is None:
                progress.poll()
                time.sleep(0.2)
        progress.poll()
        stderr = err_path.read_text(encoding="utf-8", errors="replace")
        h_rec, got, begun = parse_lines(out)
        harness = harness or h_rec
        cells += got
        if proc.returncode == 0:
            return harness or {}, cells
        if proc.returncode == 2 and not begun:
            raise HarnessError(f"the {lang} harness refused to start: {stderr.strip()[-500:]}")
        done = {f"{c['library']}/{c['task']}/{c['workload']}" for c in got}
        in_flight = [b for b in begun if b not in done]
        if not in_flight:
            raise HarnessError(f"the {lang} harness stopped between cells (exit status {proc.returncode})\n"
                               f"{stderr.strip()[-500:]}")
        name = in_flight[-1]
        lib, task, wl = name.split("/", 2)
        log(f"  {name}: the process died (exit status {proc.returncode}); continuing without it")
        cells.append({"type": "cell", "library": lib, "task": task, "workload": wl, "status": "error",
                      "detail": f"the process died (exit status {proc.returncode})", "input_bytes": None})
        skip += list(begun)
    raise HarnessError("too many harness restarts")


def library_sources(records: dict[str, dict]) -> dict[str, dict]:
    """The release tag of each of our libraries that was built from a checkout named by
    KIT_ONEYAML_<LANG>_SRC, when that checkout is at a tagged commit. Recorded now because the
    checkout is gone by the time a report is made."""
    out = {}
    for lang, rec in sorted(records.items()):
        src = os.environ.get(f"KIT_ONEYAML_{lang.upper()}_SRC")
        for lib in rec.get("libraries", []):
            if lib.get("role") != "own" or not src:
                continue
            tag = environment.release_tag(Path(src), str(lib.get("version", "")))
            if tag:
                out[lang] = {"library": lib["id"], "revision": lib["version"], "tag": tag}
    return out


def assemble(mode: str, smoke: bool, records: dict[str, dict], cells_by_lang: dict[str, list[dict]],
             metas: list[dict], load_start: float, load_end: float, seconds: float,
             expected: list[str] = ()) -> dict:
    cells = []
    for lang, recs in cells_by_lang.items():
        libs = {l["id"]: l for l in records[lang].get("libraries", [])}
        for c in recs:
            cell = {k: v for k, v in c.items() if k != "type"}
            cell["language"] = lang
            cell["role"] = (libs.get(c["library"]) or {}).get("role", "competitor")
            # A library whose API takes and returns text has no parsed document to start from, so
            # its cells do not do the task as specified (see results.non_reference_label).
            if ((libs.get(c["library"]) or {}).get("settings") or {}).get("state") == "the text":
                cell["state"] = "text"
            cells.append(cell)
    env = environment.describe(environment.git_revision(ROOT.parent))
    run_record = {"date": env["date"], "mode": mode, "smoke": smoke, "seconds": round(seconds, 1),
                  "environment": env, "load": {"before": load_start, "after": load_end},
                  "expected_languages": sorted(set(expected) | set(records))}
    sources = library_sources(records)
    if sources:
        run_record["sources"] = sources
    result = {
        "format": results.FORMAT,
        "run": run_record,
        "harnesses": {lang: {k: v for k, v in rec.items() if k != "type"} for lang, rec in records.items()},
        "workloads": [{k: m[k] for k in ("id", "kind", "tier", "description", "source", "documents",
                                         "input_bytes", "input_sha256", "tasks", "notes") if k in m}
                      for m in metas],
        "cells": cells,
    }
    taskstate.mark_text_tasks(result)
    return result


def run(mode: str, out_dir: Path, languages: list[str] | None = None, libraries: list[str] = (),
        tasks: list[str] = (), only: list[str] = (), smoke: bool | None = None,
        build_dir: Path | None = None, log=say) -> dict:
    """The whole pipeline up to the result dict. A quick run is always a smoke run; `smoke=True`
    labels a full run taken on a busy or shared machine the same way."""
    t0 = time.monotonic()
    harnesses = discover()
    langs = languages or sorted(harnesses)
    for l in langs:
        if l not in harnesses:
            raise HarnessError(f"no harness for {l!r}; have {sorted(harnesses)}")
    tiers = ("quick",) if mode == "quick" else ("quick", "full")
    out_dir.mkdir(parents=True, exist_ok=True)
    # Results left by an earlier run must not pass for this one's.
    for old in [*out_dir.glob("results*.json"), out_dir / "report.md"]:
        old.unlink(missing_ok=True)
    log(f"preparing workloads ({', '.join(tiers)})")
    metas = workloads.prepare(out_dir, tiers)
    wl_dir = out_dir / "workloads"
    build_dir = build_dir or Path(os.environ.get("CARGO_TARGET_DIR") or out_dir / "target")
    if only:
        metas = [m for m in metas if m["id"] in only]
    load_start = environment.loadavg()[0]
    # A run is meant to measure the languages it was asked for (all of them by default); a result
    # that lacks one of those says so and is not a complete run.
    expected = sorted(langs)
    # Only a run on the machine that `bin/bench-cloud` launches, from the pushed kit, counts as a
    # measurement; anywhere else the machine may be shared or the code may not be pushed.
    on_cloud = bool(os.environ.get("KIT_INSTANCE_TYPE")) and not os.environ.get("KIT_SMOKE")
    is_smoke = bool(smoke) or mode == "quick" or not on_cloud

    def assemble_for(ls: list[str]) -> dict:
        return assemble(mode, is_smoke, {l: records[l] for l in ls}, {l: cells_by_lang[l] for l in ls}, metas,
                        load_start, environment.loadavg()[0], time.monotonic() - t0, expected)

    records, cells_by_lang = {}, {}
    for l in langs:
        h = harnesses[l]
        log(f"building the {l} harness")
        build(h, build_dir)
        log(f"start {l} ({mode})")
        t_lang = time.monotonic()
        records[l], cells_by_lang[l] = run_harness(h, build_dir, wl_dir, mode, out_dir, libraries, tasks, only, log)
        log(f"end {l}: {len(cells_by_lang[l])} cells in {time.monotonic() - t_lang:.1f} s")
        # Written now, so a run that is cut short keeps every language that finished.
        write(assemble_for([l]), out_dir / f"results-{l}.json")
    return assemble_for(langs)


def write(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
