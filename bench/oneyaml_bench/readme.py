"""Generated blocks for a library's README and docs, spliced between markers.

A file marks each block with its kind:

    <!-- generated:bench-hero -->
    ...replaced on every run...
    <!-- end generated -->

The numbers, ratios, verdicts and the choice of the strongest competitor all come from
`results.compare_rows`, and cells and charts from `report`, so a README can never disagree with the
evidence page built from the same result file. The layout of every block lives in the `render_*`
functions of this module.
"""

from __future__ import annotations

import math
import os
import re
import sys
from pathlib import Path

from . import report, results

BEGIN_RE = re.compile(r"<!-- generated:([A-Za-z0-9_-]*) -->")
END_MARK = "<!-- end generated -->"

# Task names in a result that the evidence page has no wording for are still shown, after these.
TASK_LABEL = {
    "typed-load": "typed load", "generic-load": "generic load", "dump": "dump",
    "document-parse": "document parse", "unchanged-emit": "unchanged emit", "edit-script": "edit script",
    "patch": "patch (RFC 6902)",
}
TASK_ORDER = list(TASK_LABEL)


class ReadmeError(Exception):
    pass


# ---- markers ----------------------------------------------------------------------------

def scan_blocks(text: str, known: set[str], where: str = "file") -> list[tuple[str, int, int]]:
    """(kind, start, end) of the text between each pair of markers, in file order. Fails on an unknown
    kind, a begin inside another block, an end with no begin, a begin with no end, or a kind used twice."""
    out: list[tuple[str, int, int]] = []
    open_kind = None
    open_at = 0
    events = sorted([(m.start(), m.end(), m.group(1)) for m in BEGIN_RE.finditer(text)]
                    + [(m.start(), m.end(), None) for m in re.finditer(re.escape(END_MARK), text)],
                    key=lambda e: e[0])
    for start, end, kind in events:
        if kind is not None:
            if kind not in known:
                raise ReadmeError(f"{where}: unknown block kind {kind!r} (known: {', '.join(sorted(known))})")
            if open_kind is not None:
                raise ReadmeError(f"{where}: nested markers: {kind!r} begins inside {open_kind!r}")
            if any(k == kind for k, _, _ in out):
                raise ReadmeError(f"{where}: duplicated block {kind!r}")
            open_kind, open_at = kind, end
        else:
            if open_kind is None:
                raise ReadmeError(f"{where}: an end marker with no begin marker (out of order or extra)")
            out.append((open_kind, open_at, start))
            open_kind = None
    if open_kind is not None:
        raise ReadmeError(f"{where}: missing end marker for block {open_kind!r}")
    return out


def splice(text: str, blocks: dict[str, str], known: set[str], where: str = "file") -> str:
    found = scan_blocks(text, known, where)
    missing = sorted(set(blocks) - {k for k, _, _ in found})
    if missing:
        raise ReadmeError(f"{where}: no marker for block {', '.join(missing)}")
    for kind, start, end in reversed(found):
        if kind in blocks:
            # Follow the line ending the file already uses at this marker.
            eol = "\r\n" if text[start:start + 2] == "\r\n" else "\n"
            text = text[:start] + eol + blocks[kind].replace("\n", eol) + eol + text[end:]
    return text


# ---- aggregation ------------------------------------------------------------------------

def geomean(values: list[float]) -> float:
    return math.exp(sum(math.log(v) for v in values) / len(values))


def featured_language(result: dict, library: str) -> str:
    langs = sorted({c["language"] for c in result["cells"] if c["library"] == library and c["role"] == "own"})
    if not langs:
        raise ReadmeError(f"library {library!r} has no cells in this result as one of ours")
    if len(langs) > 1:
        raise ReadmeError(f"library {library!r} appears in several languages: {', '.join(langs)}")
    return langs[0]


def task_rank(t: str) -> int:
    return TASK_ORDER.index(t) if t in TASK_ORDER else len(TASK_ORDER)


def task_sort(tasks) -> list[str]:
    return sorted(tasks, key=lambda t: (task_rank(t), t))


MARK = {**report.MARK, "mixed": "◆ mixed"}


def mark_of(t: dict, ratio: float) -> str:
    """The mark of a row, from the per-workload verdicts of the kit, never from an average alone: a row
    with a winning workload and a losing one is mixed, a row is a win only if some workload is a win
    and the mean agrees, and a row with a losing workload is never better than mixed."""
    if t["loss"] and t["win"]:
        return "mixed"
    if t["loss"]:
        return "loss" if ratio > 1 else "mixed"
    if t["win"]:
        return "win" if ratio < 1 else "tie"
    return "tie"


def aggregate(result: dict, library: str, rows: list[dict]) -> list[dict]:
    """One entry per task. The workloads used are the real-world ones the library has a cell for; a
    task with none of those uses every workload it has (`basis` says which). Each workload's ratio
    is ours ÷ that workload's strongest competitor; the entry's ratio is their geometric mean, which
    equals the geometric mean of our times ÷ the geometric mean of the strongest competitors' times.
    The entry's mark comes from the per-workload verdicts (see `mark_of`), for time and for memory alike.

    A workload where our cell has no number is not dropped: `missing` lists it with its status, and the
    entry says the aggregate covers the rest. A workload with no competitor answer is listed in `no_rival`."""
    kind = {w["id"]: w["kind"] for w in result["workloads"]}
    out = []
    for task in task_sort({r["task"] for r in rows}):
        trs = [r for r in rows if r["task"] == task]
        real = [r for r in trs if kind.get(r["workload"]) == "real-world"]
        used, basis = (real, "real-world") if real else (trs, "all")
        e = {"task": task, "basis": basis, "workloads": [r["workload"] for r in used], "missing": [],
             "no_rival": [], "tally": {"win": 0, "tie": 0, "loss": 0},
             "t_own": [], "t_best": [], "vs": {}, "marks": set(),
             "m_own": [], "m_best": [], "m_vs": {}, "m_tally": {"win": 0, "tie": 0, "loss": 0}}
        for r in used:
            mine = next(c for c in r["cells"] if c["library"] == library)
            ent = next(x for x in r["own"] if x["library"] == library)
            if mine["status"] != "ok":
                e["missing"].append((r["workload"], mine["status"]))
                continue
            if ent["time"] is None:
                e["no_rival"].append(r["workload"])
            else:
                best = r["best"]["time"]
                e["t_own"].append(mine["time"]["ns"])
                e["t_best"].append(best["time"]["ns"])
                e["tally"][ent["time"]["verdict"]] += 1
                e["vs"][best["library"]] = e["vs"].get(best["library"], 0) + 1
                if results.non_reference_label(best):
                    e["marks"].add(f"{best['library']}: {results.non_reference_label(best)}")
            if ent["memory"] is not None:
                bm = r["best"]["memory"]
                e["m_own"].append(max(mine["memory"]["allocated_bytes"], 1))
                e["m_best"].append(max(bm["memory"]["allocated_bytes"], 1))
                e["m_tally"][ent["memory"]["verdict"]] += 1
                e["m_vs"][bm["library"]] = e["m_vs"].get(bm["library"], 0) + 1
                if results.non_reference_label(bm):
                    e["marks"].add(f"{bm['library']}: {results.non_reference_label(bm)}")
        if e["t_own"]:
            e["own_time"], e["best_time"] = geomean(e["t_own"]), geomean(e["t_best"])
            e["ratio"] = results.ratio(e["own_time"], e["best_time"])
            e["verdict"] = mark_of(e["tally"], e["ratio"])
        else:
            e["ratio"] = e["verdict"] = None
        if e["m_own"]:
            e["own_mem"], e["best_mem"] = geomean(e["m_own"]), geomean(e["m_best"])
            e["m_ratio"] = results.ratio(e["own_mem"], e["best_mem"])
            e["m_verdict"] = mark_of(e["m_tally"], e["m_ratio"])
        else:
            e["m_ratio"] = e["m_verdict"] = None
        out.append(e)
    return out


# ---- shared pieces ----------------------------------------------------------------------

class Ctx:
    def __init__(self, result, library, kit_version, conformance, chart_dir, allow_smoke):
        results.validate(result)
        self.result, self.library, self.kit_version = result, library, kit_version
        self.conformance, self.chart_dir, self.allow_smoke = conformance, chart_dir, allow_smoke
        self.run = result["run"]
        if self.run["smoke"] and not allow_smoke:
            raise ReadmeError("this is a smoke result; a README is public, so it "
                              "is refused unless --allow-smoke is given (the block is then labelled)")
        self.lang = featured_language(result, library)
        self.harness = result["harnesses"][self.lang]
        self.rank = report.workload_order(result)
        self.cells = [c for c in result["cells"] if c["language"] == self.lang]
        every = results.compare_rows(self.cells)
        self.rows = [r for r in every if any(c["library"] == library for c in r["cells"])]
        # Rows that competitors have a cell for and our library has none: not run, and said so.
        self.not_run = [r for r in every if not any(c["library"] == library for c in r["cells"])]
        key = lambda r: (task_rank(r["task"]), self.rank.get(r["workload"], 0))  # noqa: E731
        self.rows.sort(key=key)
        self.not_run.sort(key=key)
        self.files: dict[str, str] = {}  # chart files to write: name -> text
        self.pinned = pinned_yts  # a field so that a test can stand in for the vendored suite

    def libs(self):
        return [l for l in self.harness["libraries"] if l["role"] != "control"]

    def version_of(self, lib: str) -> str:
        l = next((l for l in self.harness["libraries"] if l["id"] == lib), None)
        return f"{lib} {l['version']}" if l else lib

    def kit_text(self) -> str:
        # The bench result records the kit revision, not its version; the version comes from the
        # conformance result, which does, and failing that from the checkout that renders the block.
        rev = self.run["environment"].get("kit_revision")
        if self.conformance is not None and self.conformance.get("kit", {}).get("version"):
            return f"Kit {self.conformance['kit']['version']} (revision {rev})"
        return f"Kit revision {rev} (rendered with kit version {self.kit_version})"


def smoke_notice(ctx: Ctx) -> list[str]:
    if not ctx.run["smoke"]:
        return []
    return ["> **SMOKE RESULT.** Quick, or taken on a shared machine: not a claim about speed.", ""]


def method_strip(ctx: Ctx) -> list[str]:
    run, env, h = ctx.run, ctx.run["environment"], ctx.harness
    label = "SMOKE RESULT" if run["smoke"] else "full run"
    comps = [l for l in ctx.libs() if l["id"] != ctx.library]
    return [
        f"Method: {run['mode']} run of {run['date']}, **{label}**. {ctx.kit_text()}. "
        f"`{ctx.version_of(ctx.library)}`; competitors "
        + ", ".join(f"`{ctx.version_of(l['id'])}`" for l in comps) + ". "
        f"Runtime {h['runtime']} (target {h['target']}), measured with {h['tool']}: {h['timer']}. Memory: {h['memory']}. "
        f"{env['cpu']} ({env['cpus']} logical CPUs, {env['memory_gb']} GiB), {env['os']} ({env['machine']}), "
        f"host load (1 min) {run['load']['before']} before, "
        f"{run['load']['after']} after."]


def verdict_text(v: str | None) -> str:
    return "—" if v is None else MARK[v]


def not_run_text(ctx: Ctx) -> list[str]:
    if not ctx.not_run:
        return []
    items = ", ".join(f"{TASK_LABEL.get(r['task'], r['task'])} `{r['workload']}`" for r in ctx.not_run)
    return [f"Not run by `{ctx.library}` (competitors have a cell, we have none): {items}.", ""]


def task_name(a: dict) -> str:
    return TASK_LABEL.get(a["task"], a["task"])


def chart_tag(a: dict) -> str:
    """The words after a bar (or in place of one): the mark, and what keeps the row from being whole."""
    if a["ratio"] is None:
        why = ", ".join(f"{w} {s}" for w, s in a["missing"]) or "no competitor answer"
        return f"NOT COMPARABLE ({why})" if a["missing"] else "no rival"
    tag = "  " + MARK[a["verdict"]]
    if a["missing"]:
        tag += " (partial: " + ", ".join(f"{w} {s}" for w, s in a["missing"]) + ")"
    return tag


def hero_alt(title: str, note: str, aggs: list[dict]) -> str:
    """Alt text with every task's number or reason. It counts tasks, not workloads."""
    n = {k: sum(1 for a in aggs if a["verdict"] == k) for k in ("win", "tie", "mixed", "loss")}
    none = sum(1 for a in aggs if a["ratio"] is None)
    parts = [f"{title}. {note}",
             f"{len(aggs)} tasks: {n['win']} better, {n['tie']} level, {n['mixed']} mixed, {n['loss']} worse, "
             f"{none} not comparable."]
    items = []
    for a in aggs:
        if a["ratio"] is None:
            items.append(f"{task_name(a)} {chart_tag(a)}")
            continue
        t = a["tally"]
        used = len(a["t_own"])
        items.append(f"{task_name(a)} {report.fmt_ratio(a['ratio'])} {MARK[a['verdict']].split(' ', 1)[1]} "
                     f"(ours {report.fmt_time(a['own_time'])} vs {report.fmt_time(a['best_time'])}; workloads: "
                     f"{t['win']} better, {t['tie']} level, {t['loss']} worse of {used}"
                     + ("; partial, no number for " + ", ".join(f"{w} ({s})" for w, s in a["missing"]) if a["missing"] else "")
                     + ")")
    parts.append("; ".join(items) + ".")
    return " ".join(parts)


def chart_files(ctx: Ctx, aggs: list[dict], rel: str) -> list[str]:
    # Every task has a row; one with no number is a labelled row without a bar.
    data = [{"workload": task_name(a), "library": ctx.library, "vs": " / ".join(sorted(a["vs"])) or "—",
             "verdict": a["verdict"] or "tie", "ratio": a["ratio"], "tag": chart_tag(a),
             "own": a.get("own_time"), "rival": a.get("best_time")} for a in aggs]
    if not data:
        return []
    name = f"{ctx.library}-hero"
    title = f"{ctx.library}: time per task, ours against the strongest competitor"
    note = (f"Geometric mean over the workloads of each task; the mark comes from the per-workload verdicts. "
            f"{report.CHART_NOTE}")
    alt = hero_alt(title, note, aggs)
    for theme in report.THEMES:
        ctx.files[f"{name}-{theme}.svg"] = report.svg_chart(title, note, data, theme, report.fmt_time, alt)
    return [report.picture(name, alt, rel), ""]


# ---- the blocks -------------------------------------------------------------------------

def render_hero(ctx: Ctx, chart_rel: str, **_) -> str:
    aggs = aggregate(ctx.result, ctx.library, ctx.rows)
    out = smoke_notice(ctx) + chart_files(ctx, aggs, chart_rel)
    body = []
    for a in aggs:
        cover = []
        if a["missing"]:
            cover.append("no number: " + ", ".join(f"`{w}` ({s})" for w, s in a["missing"]))
        if a["no_rival"]:
            cover.append("no competitor answer: " + ", ".join(f"`{w}`" for w in a["no_rival"]))
        if a["ratio"] is None:
            body.append([task_name(a), "—", "—", "—", "—", "—",
                         "NOT COMPARABLE" if a["missing"] else "no rival", "; ".join(cover) or "—"])
            continue
        used = len(a["t_own"])
        names = lambda d, ver: ", ".join(  # noqa: E731
            f"`{ctx.version_of(k) if ver else k}`" + (f" ({v} of {sum(d.values())})" if len(d) > 1 else "")
            for k, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0])))
        vs = names(a["vs"], True)
        if a["marks"]:
            vs += f"{report.MARK_NOT_BEST} ({'; '.join(sorted(a['marks']))})"
        if a["m_ratio"] is None:
            mem = "—"
        else:
            mt = a["m_tally"]
            mem = (f"{report.fmt_bytes(a['own_mem'])} vs {report.fmt_bytes(a['best_mem'])}, "
                   f"{report.fmt_ratio(a['m_ratio'])} {verdict_text(a['m_verdict'])}; "
                   f"{mt['win']} better, {mt['tie']} level, {mt['loss']} worse of {sum(mt.values())}; "
                   f"least allocated: {names(a['m_vs'], False)}")
        mark = verdict_text(a["verdict"]) + (" (partial: a workload has no number)" if a["missing"] else "")
        t = a["tally"]
        per = f"{t['win']} better, {t['tie']} level, {t['loss']} worse of {used}" + (
            f"; {'; '.join(cover)}" if cover else "")
        body.append([task_name(a), report.fmt_time(a["own_time"]), vs,
                     report.fmt_time(a["best_time"]), f"{report.fmt_ratio(a['ratio'])}", mem, mark, per])
    for task in task_sort({r["task"] for r in ctx.not_run} - {a["task"] for a in aggs}):
        body.append([TASK_LABEL.get(task, task), "—", "—", "—", "—", "—", "NOT RUN",
                     "competitors have cells, we have none"])
    kinds = {a["basis"] for a in aggs}
    out.append(report.md_table(
        ["Task", f"`{ctx.library}` time (geometric mean)", "Strongest competitors",
         "Strongest competitors' time (geometric mean)", "Ratio (ours ÷ theirs)",
         "Peak memory (geometric mean, ours vs smallest)", "Verdict", "Per workload"], body))
    out.append("")
    out += not_run_text(ctx)
    basis = {a["task"]: a for a in aggs}
    nrw = sorted({TASK_LABEL.get(t, t) for t, a in basis.items() if a["basis"] != "real-world"})
    out.append("Each row is the geometric mean, over the " + ("real-world workloads" if kinds <= {"real-world"} else
               "real-world workloads (all workloads for " + ", ".join(nrw) + ", which have no real-world one)")
               + " the task has, of our time and of the time of the strongest competitor in each workload; "
               "the ratio is the same as the geometric mean of the per-workload ratios, and below 1.0× is better for us. "
               "Where several competitors were strongest the column names them all, with how often. A † names a "
               "competitor that did less than the full task; it still counts. A workload where our library has no "
               "number is listed and the row is marked as partial, never counted as a win. "
               "The verdict is never better than the per-workload verdicts allow: a row with a losing workload is "
               "marked mixed or loss, and a row with no winning workload is never marked a win. Time and memory "
               "each have their own count of better, level and worse workloads, with the kit's noise floor.")
    out.append("")
    out += method_strip(ctx)
    return "\n".join(out)


def render_full(ctx: Ctx, **_) -> str:
    libs = [ctx.library] + [l["id"] for l in ctx.libs() if l["id"] != ctx.library]
    present = {c["library"] for c in ctx.cells}
    libs = [l for l in libs if l in present]
    wl = {w["id"]: w for w in ctx.result["workloads"]}
    out = smoke_notice(ctx)
    for metric, title, cellfn in (("time", "Time per operation", report.time_cell),
                                  ("memory", "Peak / allocated heap per operation", report.memory_cell)):
        body = []
        for r in ctx.rows:
            by = {c["library"]: c for c in r["cells"]}
            ent = next(x for x in r["own"] if x["library"] == ctx.library)
            if by[ctx.library]["status"] != "ok":
                rel = f"no number ({by[ctx.library]['status']})"
            elif ent[metric] is None:
                rel = "no rival"
            else:
                m = ent[metric]
                rel = (f"{report.fmt_ratio(m['ratio'])} of `{m['vs']}`"
                       + (f"{report.MARK_NOT_BEST} ({m['vs_mark']})" if m.get("vs_mark") else "")
                       + f" {MARK[m['verdict']]}")
            body.append([TASK_LABEL.get(r["task"], r["task"]), f"`{r['workload']}`",
                         report.fmt_size(wl[r["workload"]]["input_bytes"])]
                        + ["—" if l not in by else ("unsupported" if by[l]["status"] == "unsupported" else cellfn(by[l]))
                           for l in libs] + [rel])
        out.append(f"**{title}**\n")
        out.append(report.md_table(["Task", "Workload", "Input"] + [f"`{l}`" for l in libs]
                                   + ["Ours vs strongest competitor"], body))
        out.append("")
    out += not_run_text(ctx)
    out.append("Relative figures are ours ÷ the strongest competitor in that row, so below 1.0× is better for us "
               "and ▼ marks a loss. A † names a cell that did less than the full task; it still counts. Cells "
               "without a number say why.")
    out.append("")
    out += method_strip(ctx)
    return "\n".join(out)


def pinned_yts() -> tuple[str, int]:
    """The scored yaml-test-suite release and how many kit cases it makes, from the vendored data."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    from oneyaml_kit import cases  # noqa: E402

    return cases.YTS_RELEASE, len(cases.load_yts())


def checked_conformance(ctx: Ctx) -> tuple[dict, list[dict], list[dict]]:
    """The conformance result, its yaml-test-suite cases and its own cases, once it is fit to publish: for this
    library, from a complete run (not filtered to some cases), with no unlisted, stale or unknown deviation,
    at the pinned release and with every case of it. A result that is not is refused, never summarised."""
    conf = ctx.conformance
    if conf is None:
        raise ReadmeError("a conformance block needs a conformance result for the library")
    name = conf.get("adapter", {}).get("name")
    if name != ctx.library:
        raise ReadmeError(f"the conformance result is for {name!r}, not {ctx.library!r}")
    if not conf.get("run", {}).get("complete"):
        raise ReadmeError("the conformance result is from a filtered run (run.complete is not true)")
    if not conf.get("deviations", {}).get("ok"):
        raise ReadmeError("the conformance result has unlisted, stale or unknown deviations (deviations.ok is not true)")
    suite = [c for c in conf["cases"] if c.get("suite") == "yaml-test-suite"]
    release = conf["kit"]["suites"]["yaml-test-suite"]["release"]
    pin_release, pin_count = ctx.pinned()
    if release != pin_release:
        raise ReadmeError(f"the conformance result is at yaml-test-suite {release}, the pin is {pin_release}")
    if len(suite) != pin_count:
        raise ReadmeError(f"the conformance result has {len(suite)} yaml-test-suite cases, the pinned set has {pin_count}")
    return conf, suite, [c for c in conf["cases"] if c.get("suite") == "own"]


def render_conformance_line(ctx: Ctx, conformance_link: str, **_) -> str:
    conf, suite, own = checked_conformance(ctx)
    release = conf["kit"]["suites"]["yaml-test-suite"]["release"]

    def n(cs, mode, key=None):
        return sum((c[key] if key else c)[mode] == "pass" for c in cs)
    text = f"yaml-test-suite {release}: {n(suite, 'loose')}/{len(suite)} loose, {n(suite, 'strict')}/{len(suite)} strict."
    corrected = [c for c in suite if "corrected" in c]
    if corrected:
        text += (f" Against the kit's corrected expectations for {len(corrected)} of them: "
                 f"{n(corrected, 'loose', 'corrected')}/{len(corrected)} loose, "
                 f"{n(corrected, 'strict', 'corrected')}/{len(corrected)} strict.")
    if own:
        total = conf["kit"]["suites"].get("own", {}).get("cases")
        scope = ("" if total is None else
                 ", all of them including the slow ones" if total == len(own) else
                 f", the fast set: {len(own)} of {total}, the slow ones were not run")
        text += f" Kit's own cases{scope}: {n(own, 'loose')}/{len(own)} loose, {n(own, 'strict')}/{len(own)} strict."
    return text + f" [Details]({conformance_link})"


def render_deviations(ctx: Ctx, **_) -> str:
    conf, _suite, _own = checked_conformance(ctx)
    entries = conf["deviations"]["entries"]
    if not entries:
        return "No known deviations."
    body = [[f"`{e['case']}`", e.get("expectation", "primary"), e["mode"], e["verdict"],
             e["reason"].replace("|", "\\|").replace("\n", " ")] for e in entries]
    return (f"{len(entries)} known deviations:\n\n"
            + report.md_table(["Case", "Expectation", "Mode", "Verdict", "Reason"], body))


KINDS = {"bench-hero": render_hero, "conformance-line": render_conformance_line,
         "bench-full": render_full, "conformance-deviations": render_deviations}


# ---- update -----------------------------------------------------------------------------

def update(ctx: Ctx, targets: list[Path], charts: str, docs_conformance: Path) -> dict[Path, bytes]:
    """The new bytes of every target file that holds blocks; nothing is written here. A target with no
    markers is skipped only when it is not the first (the README); the first must have at least one."""
    known = set(KINDS)
    chart_abs = targets[0].parent / charts
    new: dict[Path, bytes] = {}
    for i, path in enumerate(targets):
        if not path.exists():
            if i == 0:
                raise ReadmeError(f"{path} does not exist")
            continue
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ReadmeError(f"{path} is not UTF-8: {e}") from None
        found = scan_blocks(text, known, str(path))
        if not found:
            if i == 0:
                raise ReadmeError(f"{path}: no generated markers; add e.g. <!-- generated:bench-hero --> "
                                  f"and {END_MARK}")
            continue
        rel = lambda p: Path(os.path.relpath(p, path.parent)).as_posix()  # noqa: E731
        blocks = {k: KINDS[k](ctx, chart_rel=rel(chart_abs), conformance_link=rel(docs_conformance))
                  for k, _, _ in found}
        new[path] = splice(text, blocks, known, str(path)).encode("utf-8")
    return new


def check_and_write(ctx: Ctx, new: dict[Path, bytes], chart_dir: Path, hits) -> list[Path]:
    """Public-text check on everything generated, then writes. Returns the files that changed."""
    bad = [h for t in list(ctx.files.values()) + [b.decode() for b in new.values()] for h in hits(t)]
    if bad:
        raise ReadmeError("generated text fails the public-text check: " + "; ".join(bad))
    # Every new file is written beside its target first and then renamed over it, so an error part way
    # leaves each file whole (old or new), and the renames come last.
    todo: list[tuple[Path, bytes]] = []
    for name, svg in ctx.files.items():
        p = chart_dir / name
        if not p.exists() or p.read_bytes() != svg.encode("utf-8"):
            todo.append((p, svg.encode("utf-8")))
    todo += [(p, data) for p, data in new.items() if p.read_bytes() != data]
    staged = []
    try:
        for p, data in todo:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".new")
            tmp.write_bytes(data)
            staged.append((tmp, p))
        for tmp, p in staged:
            os.replace(tmp, p)
    finally:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
    return [p for p, _ in todo]
