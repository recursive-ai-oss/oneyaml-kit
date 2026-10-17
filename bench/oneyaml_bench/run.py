"""Builds each language's harness, runs it, and assembles one result file.

A harness is a program with a fixed command line and output (docs/benchmarks.md):

    <command> run --workloads DIR --out FILE --mode quick|full
              [--library ID]... [--task ID]... [--workload ID]... [--skip LIB/TASK/WORKLOAD]...

It writes JSON lines: one `harness` record, then for each cell a `begin` mark followed by a
`cell` record. The mark lets this driver name the cell that killed the process, record it as an
`error` and start the harness again past it.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import environment, results, taskstate, workloads

ROOT = Path(__file__).resolve().parents[1]
MAX_RESTARTS = 50
# A hang guard, not a bound on any real cell: the slowest cell of the full runs so far took 134 s, so
# this is about 13 times that. The same limit applies to every library and language.
CELL_LIMIT_SECONDS = 1800.0


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


def text_hits(text: str) -> list[str]:
    """The kit's public-text check over a string (the same one the end-of-run check uses)."""
    sys.path.insert(0, str(ROOT.parent / "tools"))
    from oneyaml_kit import public_text  # noqa: E402

    return [m.group(0) for line in text.split("\n") if (m := public_text.PATTERN.search(line))]


def record_hits(value, field: str = "harness") -> list[tuple[str, str]]:
    """(field, offending text) for every string in a harness record, keys included: all of it is published."""
    if isinstance(value, str):
        return [(field, h) for h in text_hits(value)]
    if isinstance(value, dict):
        return [x for k, v in value.items() for x in record_hits(k, f"{field}.{k} (key)") + record_hits(v, f"{field}.{k}")]
    if isinstance(value, list):
        return [x for i, v in enumerate(value) for x in record_hits(v, f"{field}[{i}]")]
    return []


class Progress:
    """Follows the harness output file while the harness runs and prints one line per finished cell."""

    def __init__(self, path: Path, lang: str, log):
        self.path, self.lang, self.log = path, lang, log
        self.pos, self.began, self.n = 0, time.monotonic(), 0
        self.checked = False

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
            if rec.get("type") == "harness" and not self.checked:
                # The record goes into the published method text, and is known before the first cell:
                # refuse it now rather than after a long measurement.
                self.checked = True
                hits = record_hits({k: v for k, v in rec.items() if k != "type"})
                if hits:
                    raise HarnessError(f"the {self.lang} harness record holds text that cannot be published; "
                                       "stopped before measuring:\n  "
                                       + "\n  ".join(f"{f}: {t!r}" for f, t in hits))
            if rec.get("type") == "begin":
                self.began = time.monotonic()
            elif rec.get("type") == "cell":
                self.n += 1
                self.log(f"  {self.lang} {rec['library']} {rec['task']} {rec['workload']}: {rec['status']} "
                         f"({time.monotonic() - self.began:.1f} s)")


def kill_group(proc: subprocess.Popen) -> None:
    """Kills every process in the harness's group, children and grandchildren included. The group
    outlives its leader, so this also finds what a harness that exited or crashed left behind."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass


class Terminated(SystemExit):
    """Raised by the signal handler so that cleanup runs on the way out."""


@contextlib.contextmanager
def _terminate_as_exception():
    # SIGTERM would end the process with no cleanup at all. SIGINT is set too: a driver started in the
    # background by a launcher (nohup, or a shell's `&`) inherits it as ignored, and would never stop.
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum, frame):
        raise Terminated(128 + signum)

    signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    old = {s: signal.signal(s, handler) for s in signals}
    try:
        yield
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def run_harness(h: dict, build_dir: Path, workloads_dir: Path, mode: str, out_dir: Path,
                libraries=(), tasks=(), only=(), log=say, on_poll=None,
                cell_limit: float = CELL_LIMIT_SECONDS) -> tuple[dict, list[dict]]:
    """Runs the harness to completion, restarting past any cell that kills it or runs past `cell_limit`
    seconds. `on_poll` is called from the loop that already waits on the harness, so watching the
    machine adds no thread. The harness runs in its own process group, which is killed when the
    harness ends by any route (including this driver being terminated)."""
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
        timed_out = False
        with open(err_path, "wb") as err, _terminate_as_exception():
            proc = subprocess.Popen(cmd, cwd=h["_dir"], stdout=subprocess.DEVNULL, stderr=err,
                                    start_new_session=True)
            try:
                while proc.poll() is None:
                    progress.poll()
                    if on_poll:
                        on_poll()
                    if time.monotonic() - progress.began > cell_limit:
                        timed_out = True
                        break
                    time.sleep(0.2)
            finally:
                kill_group(proc)
        progress.poll()
        stderr = err_path.read_text(encoding="utf-8", errors="replace")
        h_rec, got, begun = parse_lines(out)
        harness = harness or h_rec
        cells += got
        if proc.returncode == 0 and not timed_out:
            return harness or {}, cells
        if proc.returncode == 2 and not begun and not timed_out:
            raise HarnessError(f"the {lang} harness refused to start: {stderr.strip()[-500:]}")
        done = {f"{c['library']}/{c['task']}/{c['workload']}" for c in got}
        in_flight = [b for b in begun if b not in done]
        if not in_flight:
            what = (f"ran past the limit of {cell_limit:g} s before its first cell" if timed_out
                    else f"stopped between cells (exit status {proc.returncode})")
            raise HarnessError(f"the {lang} harness {what}\n{stderr.strip()[-500:]}")
        name = in_flight[-1]
        lib, task, wl = name.split("/", 2)
        if timed_out:
            detail = f"the cell ran past the limit of {cell_limit:g} s and was stopped"
            log(f"  {name}: {detail}; continuing without it")
        else:
            detail = f"the process died (exit status {proc.returncode})"
            log(f"  {name}: the process died (exit status {proc.returncode}); continuing without it")
        cells.append({"type": "cell", "library": lib, "task": task, "workload": wl, "status": "error",
                      "detail": detail, "input_bytes": None})
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


def library_revisions(langs) -> dict[str, dict]:
    """The revision each of our libraries will be recorded under, for every language whose build is
    pointed at a checkout by KIT_ONEYAML_<LANG>_SRC. The rule lives in tools/library-rev.sh, which
    the build scripts also call, so what is checked here is what gets stamped into the build.
    `origin` is "supplied" (named through KIT_ONEYAML_<LANG>_REV), "git" or "unknown"."""
    out = {}
    for lang in sorted(langs):
        src = os.environ.get(f"KIT_ONEYAML_{lang.upper()}_SRC")
        if not src:
            continue
        r = subprocess.run(["sh", str(ROOT.parent / "tools" / "library-rev.sh"), lang.upper(), src],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise HarnessError(f"cannot name the {lang} library revision: {r.stderr.strip()}")
        rev, origin = r.stdout.split()
        out[lang] = {"revision": rev, "origin": origin}
    return out


def unpublishable(revs: dict[str, dict]) -> dict[str, dict]:
    """The revisions a published result may not carry: an export with no history ("devel") or a
    checkout with uncommitted changes ("+modified"), unless the revision was supplied."""
    return {l: v for l, v in revs.items()
            if v["origin"] != "supplied" and (v["revision"] == "devel" or v["revision"].endswith("+modified"))}


def assemble(mode: str, smoke: bool, records: dict[str, dict], cells_by_lang: dict[str, list[dict]],
             metas: list[dict], load_start: float, load_end: float, seconds: float,
             expected: list[str] = (), load_peak: float | None = None, revisions: dict | None = None,
             cell_limit: float | None = None) -> dict:
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
    if load_peak is not None:
        run_record["load"]["peak"] = max(load_peak, load_start, load_end)
    if cell_limit is not None:
        run_record["cell_limit_seconds"] = cell_limit
    if revisions:
        run_record["library_revisions"] = {l: v for l, v in revisions.items() if l in records}
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
        build_dir: Path | None = None, log=say, cell_limit: float = CELL_LIMIT_SECONDS) -> dict:
    """The whole pipeline up to the result dict. A quick run is always a smoke run; `smoke=True`
    (or KIT_SMOKE in the environment) labels a full run the same way. A full run is a measurement
    wherever it ran; the result records where, and how busy the machine was."""
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
    # A full run counts as a measurement wherever it ran; the result says where and how busy the host
    # was, so a reader can judge it. Quick mode and an explicit request are smoke.
    is_smoke = bool(smoke) or mode == "quick" or bool(os.environ.get("KIT_SMOKE"))
    peak = [load_start]
    revisions = library_revisions(langs)
    bad = unpublishable(revisions)
    if bad and mode == "full" and not is_smoke:
        what = ", ".join(f"{l}: {v['revision']}" for l, v in sorted(bad.items()))
        raise HarnessError(
            f"refusing a full run: the library revision would be recorded as {what}. A full run is meant for "
            "publication and must name the library commit. Build from a clean checkout; for a source with no "
            "git history, name its commit as KIT_ONEYAML_GO_REV=<commit> (the same for CSHARP); or add "
            "--smoke, which labels the result a smoke result.")

    def sample_load() -> None:
        # One read of /proc/loadavg per poll of the running harness: cheap, and on no thread of its own.
        peak[0] = max(peak[0], environment.loadavg()[0])

    def assemble_for(ls: list[str]) -> dict:
        return assemble(mode, is_smoke, {l: records[l] for l in ls}, {l: cells_by_lang[l] for l in ls}, metas,
                        load_start, environment.loadavg()[0], time.monotonic() - t0, expected,
                        max(peak[0], environment.loadavg()[0]), revisions, cell_limit)

    records, cells_by_lang = {}, {}
    for l in langs:
        h = harnesses[l]
        log(f"building the {l} harness")
        build(h, build_dir)
        log(f"start {l} ({mode})")
        t_lang = time.monotonic()
        records[l], cells_by_lang[l] = run_harness(h, build_dir, wl_dir, mode, out_dir, libraries, tasks, only, log, sample_load, cell_limit)
        if mode == "full" and not is_smoke and l in revisions:
            # The revision was read before the build; the build stamps what it found. Both must agree, or the
            # tree changed in between and the result would name a commit it did not measure.
            stamped = {lib["id"]: str(lib.get("version")) for lib in records[l].get("libraries", [])
                       if lib.get("role") == "own"}
            wrong = {k: v for k, v in stamped.items() if v != revisions[l]["revision"]}
            if wrong:
                raise HarnessError(f"refusing a full run: the {l} library was built as {wrong} but its revision was "
                                   f"read as {revisions[l]['revision']}; the source changed during the run")
        log(f"end {l}: {len(cells_by_lang[l])} cells in {time.monotonic() - t_lang:.1f} s")
        # Written now, so a run that is cut short keeps every language that finished.
        write(assemble_for([l]), out_dir / f"results-{l}.json")
    return assemble_for(langs)


def write(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
