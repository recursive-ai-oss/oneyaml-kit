"""Drives a language's harness: build it, run it under watch, survive its crashes, and
assemble the result file.

A harness is a program with a fixed command line and output (docs/benchmarks.md):

    <command> run --workloads DIR --out FILE --mode quick|full
              [--library ID]... [--task ID]... [--workload ID]... [--skip LIB/TASK/WORKLOAD]...
    <command> once --workloads DIR --library ID --task ID --workload ID

`run` writes JSON lines: one `harness` record, then for each cell a `begin` mark followed
by a `cell` record. The mark lets this driver name the cell that killed the process.

A harness whose memory figure differs between processes declares `"memory_processes": {"full": 5}` in its
harness.json (the number of processes per mode). After its run the driver starts it again, that many times
less one, with `--mode memory`: the guard, the warm-up and the memory pass of every cell that was measured, and no
timing. Each cell then carries the memory figures of every process (see results.merge_memory_processes).
"""

from __future__ import annotations

import json
import os
import re
import resource
import shutil
import subprocess
import time
from pathlib import Path

from . import environment, results, workloads

ROOT = Path(__file__).resolve().parents[1]
STALL_SECONDS = {"quick": 120, "full": 1800, "memory": 1800}
ADDRESS_SPACE_LIMIT = 24 * 1024 ** 3


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


def command(h: dict, build_dir: Path, pin_cpu: int | None) -> list[str]:
    cmd = [_subst(a, build_dir) for a in h["command"]]
    if pin_cpu is not None and shutil.which("taskset"):
        cmd = ["taskset", "-c", str(pin_cpu)] + cmd
    return cmd


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_LIMIT, ADDRESS_SPACE_LIMIT))


def parse_lines(path: Path) -> tuple[dict | None, list[dict], list[str]]:
    """(harness record, cell records, names of begun cells) from a harness output file."""
    harness, cells, begun = None, [], []
    if not path.exists():
        return harness, cells, begun
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a line cut short by a crash
        t = rec.get("type")
        if t == "harness":
            harness = harness or rec
        elif t == "begin":
            begun.append(rec["cell"])
        elif t == "cell":
            cells.append(rec)
    return harness, cells, begun


def run_harness(h: dict, build_dir: Path, workloads_dir: Path, mode: str, out_dir: Path,
                sampler: environment.Sampler | None, pin_cpu: int | None = None,
                libraries: list[str] = (), tasks: list[str] = (), only: list[str] = (),
                log=print, skip_cells: list[str] = ()) -> tuple[dict, list[dict], int]:
    """Runs the harness to completion, restarting past any cell that kills it.

    Returns the harness record, every cell, and the number of restarts.
    """
    lang = h["language"]
    harness, cells, restarts = None, [], 0
    skip: list[str] = list(skip_cells)
    part = 0
    while True:
        out = out_dir / f"{lang}.part{part}.jsonl"
        out.unlink(missing_ok=True)
        cmd = command(h, build_dir, pin_cpu) + ["run", "--workloads", str(workloads_dir), "--out", str(out), "--mode", mode]
        for kind, vals in (("--library", libraries), ("--task", tasks), ("--workload", only), ("--skip", skip)):
            for v in vals:
                cmd += [kind, v]
        proc = subprocess.Popen(cmd, cwd=h["_dir"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, preexec_fn=_limits)
        if sampler is not None:
            sampler.watch(proc.pid)
        last_size, last_change = -1, time.monotonic()
        stalled = False
        while proc.poll() is None:
            time.sleep(0.25)
            size = out.stat().st_size if out.exists() else 0
            if size != last_size:
                last_size, last_change = size, time.monotonic()
            elif time.monotonic() - last_change > STALL_SECONDS[mode]:
                proc.kill()
                stalled = True
        stderr = ""
        if proc.stderr:
            stderr = proc.stderr.read()
            proc.stderr.close()
        h_rec, got, begun = parse_lines(out)
        harness = harness or h_rec
        cells += got
        if proc.returncode == 0 and not stalled:
            return harness or {}, cells, restarts
        if proc.returncode == 2 and not begun:
            raise HarnessError(f"the {lang} harness refused to start: {stderr.strip()[-500:]}")
        # Find the cell that was in flight.
        done = {f"{c['library']}/{c['task']}/{c['workload']}" for c in got}
        in_flight = [b for b in begun if b not in done]
        why = "no output for too long" if stalled else f"the process died (exit status {proc.returncode})"
        if not in_flight:
            raise HarnessError(f"the {lang} harness stopped between cells: {why}\n{stderr.strip()[-500:]}")
        name = in_flight[-1]
        lib, task, wl = name.split("/", 2)
        log(f"  {name}: {why}; continuing without it")
        cells.append({"type": "cell", "library": lib, "task": task, "workload": wl, "status": "error",
                      "detail": why, "input_bytes": None})
        skip += list(begun)
        skip.append(name)
        restarts += 1
        part += 1
        if restarts > 50:
            raise HarnessError("too many harness restarts")


CALLGRIND_TOTAL = re.compile(r"Collected\s*:\s*(\d+)")


def count_instructions(h: dict, build_dir: Path, workloads_dir: Path, cell: dict, tmp: Path) -> int | None:
    """Instructions executed by one iteration of a cell, via callgrind restricted to the
    measured region. None when the tool is missing or the run fails."""
    if not shutil.which("valgrind"):
        return None
    region = h.get("counters", {}).get("region")
    if not region:
        return None
    cmd = ["valgrind", "--tool=callgrind", f"--toggle-collect={region}", "--collect-atstart=no",
           f"--callgrind-out-file={tmp / 'callgrind.out'}"] + command(h, build_dir, None) + [
        "once", "--workloads", str(workloads_dir), "--library", cell["library"],
        "--task", cell["task"], "--workload", cell["workload"]]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=h["_dir"])
    m = CALLGRIND_TOTAL.search(r.stderr)
    return int(m.group(1)) if r.returncode == 0 and m else None


def selection_of(languages: list[str], libraries: list[str], tasks: list[str], only: list[str],
                 offered: dict[str, list[str]], all_tasks: list[str], all_workloads: list[str]) -> dict:
    """What a run was narrowed to, and what that left out of the full set the harnesses offer.

    `offered` maps each language that ran to every library its harness offers. A language that
    was not run at all is not "left out": a result for one language is complete for that language.
    A filter that still names everything leaves nothing out.
    """
    left = {
        "libraries": sorted(f"{lang}/{lib}" for lang, libs in offered.items() for lib in libs
                            if libraries and lib not in libraries),
        "tasks": sorted(t for t in all_tasks if tasks and t not in tasks),
        "workloads": sorted(w for w in all_workloads if only and w not in only),
    }
    return {"languages": sorted(languages) or "all", "libraries": sorted(libraries) or "all",
            "tasks": sorted(tasks) or "all", "workloads": sorted(only) or "all", "left_out": left}


def assemble(mode: str, harness_records: dict[str, dict], cells_by_lang: dict[str, list[dict]],
             metas: list[dict], samples: list[float], load_start: float, load_end: float,
             seconds: float, restarts: int, expected_cells: int, pinned_cpu: int | None,
             counters: bool, selection: dict | None = None) -> dict:
    """`selection` is what the run was narrowed to, from `selection_of`; None means nothing was left out."""
    selection = selection or selection_of([], [], [], [], {}, [], [])
    cells = []
    for lang, recs in cells_by_lang.items():
        libs = {l["id"]: l for l in harness_records[lang].get("libraries", [])}
        roles = {i: l["role"] for i, l in libs.items()}
        for c in recs:
            cell = {k: v for k, v in c.items() if k != "type"}
            cell["language"] = lang
            cell["role"] = roles.get(c["library"], "competitor")
            # A library whose API takes and returns text has no parsed document to start from, so
            # its cells do not do the task as specified (see results.non_reference_label).
            if ((libs.get(c["library"]) or {}).get("settings") or {}).get("state") == "the text":
                cell["state"] = "text"
            cells.append(cell)
    selftest = any(h.get("selftest_build") for h in harness_records.values())
    # A cell that crashed or stalled was not measured, so the run did not finish what it set out to.
    complete = len(cells) >= expected_cells and not any(c["status"] == "error" for c in cells)
    env = environment.describe(environment.git_revision(ROOT), pinned_cpu)
    pub = results.assess_publishability(mode, samples, load_start, load_end, complete, selftest,
                                        pinned_cpu, env["governor"],
                                        left_out=selection["left_out"])
    res = {
        "format": results.FORMAT,
        "run": {
            "date": env["date"], "mode": mode, "seconds": round(seconds, 1), "restarts": restarts,
            "counters": counters, "environment": env, "publishability": pub,
            "selection": {k: v for k, v in selection.items()},
            "load": {"before": load_start, "after": load_end},
        },
        "harnesses": {lang: {k: v for k, v in rec.items() if k != "type"} for lang, rec in harness_records.items()},
        "workloads": [{k: m[k] for k in ("id", "kind", "tier", "description", "source", "documents",
                                         "input_bytes", "input_sha256", "tasks", "notes") if k in m}
                      for m in metas],
        "cells": cells,
    }
    # A cell timed before its warm-up settled is a number nobody should publish.
    results.add_reasons(res, results.unsettled_reasons(mode, cells))
    return res


def memory_process_count(h: dict, mode: str, override: int | None) -> int:
    """How many processes measure memory for this harness in this mode: what the user asked for, else what the
    harness declares, else one."""
    if override is not None:
        return max(override, 1)
    return max(int((h.get("memory_processes") or {}).get(mode, 1)), 1)


def add_memory_processes(h: dict, build_dir: Path, wl_dir: Path, out_dir: Path, sampler, pin_cpu, libraries, tasks,
                         only, cells: list[dict], n: int, log) -> None:
    """Runs the harness n-1 more times in memory mode and merges what it measured into the ok cells."""
    ok = {f"{c['library']}/{c['task']}/{c['workload']}": c for c in cells if c["status"] == "ok"}
    not_ok = [f"{c['library']}/{c['task']}/{c['workload']}" for c in cells if c["status"] != "ok"]
    extra: dict[str, list[dict]] = {k: [] for k in ok}
    failed: dict[str, str] = {}
    for i in range(n - 1):
        log(f"measuring memory in process {i + 2} of {n}")
        _, got, _ = run_harness(h, build_dir, wl_dir, "memory", out_dir, sampler, pin_cpu, libraries, tasks, only,
                                log, skip_cells=not_ok)
        seen = {}
        for c in got:
            seen[f"{c['library']}/{c['task']}/{c['workload']}"] = c
        for k in ok:
            c = seen.get(k)
            if k in failed:
                continue
            if c is None:
                failed[k] = f"not ok in memory process {i + 2}: missing (the process gave no result for it)"
            elif c["status"] != "ok" or "memory" not in c:
                failed[k] = f"not ok in memory process {i + 2}: {c['status']} {c.get('detail', '')}".rstrip()
            else:
                extra[k].append(c["memory"])
    for k, cell in ok.items():
        if k in failed:
            # A cell that did not hold up in another process has no figure to publish: a median over the
            # processes that happened to work would look complete.
            cell["status"] = "error"
            cell["detail"] = failed[k]
            for key in ("time", "memory", "variant"):
                cell.pop(key, None)
        else:
            results.merge_memory_processes(cell, extra[k])


def run(mode: str, out_dir: Path, languages: list[str] | None = None, libraries: list[str] = (),
        tasks: list[str] = (), only: list[str] = (), pin_cpu: int | None = None,
        counters: bool = False, build_dir: Path | None = None, log=print,
        memory_processes: int | None = None) -> dict:
    """The whole pipeline up to the result dict."""
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
    all_metas = metas
    if only:
        metas = [m for m in metas if m["id"] in only]
    sampler = environment.Sampler()
    load_start = environment.loadavg()[0]
    sampler.start()
    records, cells_by_lang, restarts, n_libs, mem_procs = {}, {}, 0, 0, {}
    offered: dict[str, list[str]] = {}
    try:
        for l in langs:
            h = harnesses[l]
            log(f"building the {l} harness")
            build(h, build_dir)
            log(f"measuring ({mode})")
            rec, cells, r = run_harness(h, build_dir, wl_dir, mode, out_dir, sampler, pin_cpu,
                                        libraries, tasks, only, log)
            records[l], cells_by_lang[l], restarts = rec, cells, restarts + r
            n_mem = memory_process_count(h, mode, memory_processes)
            declared = memory_process_count(h, mode, None)
            if declared > 1 or n_mem > 1:
                mem_procs[l] = {"declared": declared, "used": n_mem}
                for c in cells:
                    if c["status"] == "ok":
                        c["memory"]["processes_declared"] = declared
            if n_mem > 1:
                add_memory_processes(h, build_dir, wl_dir, out_dir, sampler, pin_cpu, libraries, tasks, only,
                                     cells, n_mem, log)
            offered[l] = [x["id"] for x in rec.get("libraries", [])]
            if libraries:
                # The record of a filtered run lists only the chosen libraries, so ask the harness
                # again, for no cells, which libraries it offers.
                probe, _, _ = run_harness(h, build_dir, wl_dir, "quick", out_dir, None, None, (), ["none"],
                                          [all_metas[0]["id"]], log)
                offered[l] = [x["id"] for x in (probe or rec).get("libraries", [])]
            n_libs += len([x for x in rec.get("libraries", []) if not libraries or x["id"] in libraries])
            if counters:
                log("counting instructions")
                for c in cells:
                    if c["status"] == "ok" and (c.get("input_bytes") or 0) <= 200_000:
                        c["instructions"] = count_instructions(h, build_dir, wl_dir, c, out_dir)
    finally:
        samples = sampler.stop()
    load_end = environment.loadavg()[0]
    wanted = sum(len([t for t in m["tasks"] if not tasks or t in tasks]) for m in metas) * max(n_libs, 1)
    res = assemble(mode, records, cells_by_lang, metas, samples, load_start, load_end,
                   time.monotonic() - t0, restarts, wanted, pin_cpu, counters,
                   selection_of(langs if languages else [], list(libraries), list(tasks), list(only), offered,
                                sorted({t for m in all_metas for t in m["tasks"]}),
                                [m["id"] for m in all_metas]))
    if mem_procs:
        res["run"]["memory_processes"] = mem_procs
        short = [f"{l} (declares {v['declared']}, used {v['used']})" for l, v in mem_procs.items()
                 if v["used"] < v["declared"]]
        if short:
            results.add_reasons(res, ["memory was measured in fewer processes than the harness declares: "
                                      + ", ".join(short)])
    return res


def write(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
