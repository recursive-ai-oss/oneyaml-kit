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

from . import environment, results, workloads

ROOT = Path(__file__).resolve().parents[1]
MAX_RESTARTS = 50


class HarnessError(Exception):
    pass


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


def run_harness(h: dict, build_dir: Path, workloads_dir: Path, mode: str, out_dir: Path,
                libraries=(), tasks=(), only=(), log=print) -> tuple[dict, list[dict]]:
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
        proc = subprocess.run(cmd, cwd=h["_dir"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        h_rec, got, begun = parse_lines(out)
        harness = harness or h_rec
        cells += got
        if proc.returncode == 0:
            return harness or {}, cells
        if proc.returncode == 2 and not begun:
            raise HarnessError(f"the {lang} harness refused to start: {proc.stderr.strip()[-500:]}")
        done = {f"{c['library']}/{c['task']}/{c['workload']}" for c in got}
        in_flight = [b for b in begun if b not in done]
        if not in_flight:
            raise HarnessError(f"the {lang} harness stopped between cells (exit status {proc.returncode})\n"
                               f"{proc.stderr.strip()[-500:]}")
        name = in_flight[-1]
        lib, task, wl = name.split("/", 2)
        log(f"  {name}: the process died (exit status {proc.returncode}); continuing without it")
        cells.append({"type": "cell", "library": lib, "task": task, "workload": wl, "status": "error",
                      "detail": f"the process died (exit status {proc.returncode})", "input_bytes": None})
        skip += list(begun)
    raise HarnessError("too many harness restarts")


def assemble(mode: str, smoke: bool, records: dict[str, dict], cells_by_lang: dict[str, list[dict]],
             metas: list[dict], load_start: float, load_end: float, seconds: float) -> dict:
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
    return {
        "format": results.FORMAT,
        "run": {"date": env["date"], "mode": mode, "smoke": smoke, "seconds": round(seconds, 1),
                "environment": env, "load": {"before": load_start, "after": load_end}},
        "harnesses": {lang: {k: v for k, v in rec.items() if k != "type"} for lang, rec in records.items()},
        "workloads": [{k: m[k] for k in ("id", "kind", "tier", "description", "source", "documents",
                                         "input_bytes", "input_sha256", "tasks", "notes") if k in m}
                      for m in metas],
        "cells": cells,
    }


def run(mode: str, out_dir: Path, languages: list[str] | None = None, libraries: list[str] = (),
        tasks: list[str] = (), only: list[str] = (), smoke: bool | None = None,
        build_dir: Path | None = None, log=print) -> dict:
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
    log(f"preparing workloads ({', '.join(tiers)})")
    metas = workloads.prepare(out_dir, tiers)
    wl_dir = out_dir / "workloads"
    build_dir = build_dir or Path(os.environ.get("CARGO_TARGET_DIR") or out_dir / "target")
    if only:
        metas = [m for m in metas if m["id"] in only]
    load_start = environment.loadavg()[0]
    records, cells_by_lang = {}, {}
    for l in langs:
        h = harnesses[l]
        log(f"building the {l} harness")
        build(h, build_dir)
        log(f"measuring {l} ({mode})")
        records[l], cells_by_lang[l] = run_harness(h, build_dir, wl_dir, mode, out_dir, libraries, tasks, only, log)
    return assemble(mode, mode == "quick" if smoke is None else (smoke or mode == "quick"),
                    records, cells_by_lang, metas, load_start, environment.loadavg()[0], time.monotonic() - t0)


def write(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
