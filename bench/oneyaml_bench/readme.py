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

import copy
import math
import os
import re
import sys
from pathlib import Path

from . import environment, reasons, report, results, taskstate

BEGIN_RE = re.compile(r"<!-- generated:([A-Za-z0-9_-]*) -->")
END_MARK = "<!-- end generated -->"

# Task names in a result that the evidence page has no wording for are still shown, after these.
TASK_LABEL = {
    "typed-load": "typed load", "generic-load": "generic load", "dump": "dump",
    "document-parse": "document parse", "unchanged-emit": "unchanged emit", "edit-script": "edit script",
    "patch": "patch (RFC 6902)",
}
TASK_ORDER = list(TASK_LABEL)
# The hero says what the unchanged-emit task measures, so that its large factor is not read as a rendering speed.
HERO_LABEL = {"unchanged-emit": "unchanged emit (write-back of an unedited document; not a rendering speed)"}
HERO_CHART_LABEL = {"unchanged-emit": "unchanged emit (not rendering)"}


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
             "t_own": [], "t_best": [], "vs": {}, "marks": set(), "own_marks": set(),
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
                if results.non_reference_label(mine):
                    e["own_marks"].add(results.non_reference_label(mine))
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
    def __init__(self, result, library, renderer_revision, conformance, chart_dir, allow_smoke, source=None,
                 chart_omit=()):
        results.validate(result)
        result = taskstate.mark_text_tasks(copy.deepcopy(result))
        self.result, self.library, self.renderer_revision = result, library, renderer_revision
        self.conformance, self.chart_dir, self.allow_smoke = conformance, chart_dir, allow_smoke
        self.run = result["run"]
        if self.run["smoke"] and not allow_smoke:
            raise ReadmeError("this is a smoke result; a README is public, so it "
                              "is refused unless --allow-smoke is given (the block is then labelled)")
        self.lang = featured_language(result, library)
        self.harness = result["harnesses"][self.lang]
        self.tag = self.release_tag(source)
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
        # Tasks left out of the hero and chart blocks; the full page keeps every task.
        self.chart_omit = frozenset(chart_omit)
        known = sorted({r["task"] for r in every})
        unknown = sorted(self.chart_omit - set(known))
        if unknown:
            raise ReadmeError(f"cannot leave out unknown task {', '.join(unknown)} (the tasks in this result: "
                              f"{', '.join(known)})")

    def libs(self):
        return [l for l in self.harness["libraries"] if l["role"] != "control"]

    def release_tag(self, source) -> str | None:
        """The release tag of our library's revision: the one the run recorded, else the one the checkout
        at `source` has at that same commit. None when neither knows; a tag is never guessed."""
        ours = next((l for l in self.harness["libraries"] if l["id"] == self.library), None)
        if ours is None:
            return None
        rec = (self.run.get("sources") or {}).get(self.lang) or {}
        if rec.get("library") == self.library and rec.get("revision") == ours["version"] and rec.get("tag"):
            return rec["tag"]
        return environment.release_tag(Path(source), str(ours["version"])) if source else None

    def version_of(self, lib: str) -> str:
        l = next((l for l in self.harness["libraries"] if l["id"] == lib), None)
        if not l:
            return lib
        if lib == self.library and self.tag:
            return f"{lib} {self.tag} ({l['version']})"
        return f"{lib} {l['version']}"

    def kit_text(self) -> str:
        # The bench result records the kit revision it ran at, not a version; a conformance result adds the
        # kit version it ran with. The revision of the code that wrote this text is named when it differs
        # from the run's, so a reader is never given a release-looking number for it.
        rev = self.run["environment"].get("kit_revision")
        if self.conformance is not None and self.conformance.get("kit", {}).get("version"):
            text = f"Kit {self.conformance['kit']['version']} (revision {rev})"
        else:
            text = f"Kit revision {rev}"
        if self.renderer_revision and self.renderer_revision != rev:
            text += f"; text generated by kit revision {self.renderer_revision}"
        return text


def smoke_notice(ctx: Ctx) -> list[str]:
    out = []
    missing = results.missing_languages(ctx.result)
    if missing:
        out += [f"> **INCOMPLETE RUN.** No results for {', '.join(missing)}.", ""]
    if ctx.run["smoke"]:
        out += ["> **SMOKE RESULT.** Quick, or marked a smoke run: not a claim about speed.", ""]
    return out


def method_strip(ctx: Ctx) -> list[str]:
    run, env, h = ctx.run, ctx.run["environment"], ctx.harness
    # The mode is written once: a smoke result is marked after it, any other run is the mode in bold.
    what = f"{run['mode']} run of {run['date']}, **SMOKE RESULT**" if run["smoke"] else f"**{run['mode']} run** of {run['date']}"
    comps = [l for l in ctx.libs() if l["id"] != ctx.library]
    return [
        f"Method: {what}. {ctx.kit_text()}. "
        f"`{ctx.version_of(ctx.library)}`; competitors "
        + ", ".join(f"`{ctx.version_of(l['id'])}`" for l in comps) + ". "
        f"Runtime {h['runtime']} (target {h['target']}), measured with {h['tool']}: {report.timer_text(h, run['mode'])}. Memory: {h['memory']}. "
        + host_text(run, env)]


def host_text(run: dict, env: dict) -> str:
    local = report.local_host_text(run)
    if local:
        return f"{env['cpu']} ({env['memory_gb']} GiB), {env['os']} ({env['machine']}), {local}."
    return (f"{env['cpu']} ({env['cpus']} logical CPUs, {env['memory_gb']} GiB), {env['os']} ({env['machine']}), "
            f"host load (1 min) {run['load']['before']} before, {run['load']['after']} after.")


def verdict_text(v: str | None) -> str:
    return "—" if v is None else MARK[v]


def not_run_text(ctx: Ctx, rows: list[dict] | None = None) -> list[str]:
    rows = ctx.not_run if rows is None else rows
    if not rows:
        return []
    items = ", ".join(f"{TASK_LABEL.get(r['task'], r['task'])} `{r['workload']}`" for r in rows)
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


def fmt_factor(n: float) -> str:
    """A factor to three significant figures, with thousands separated: 2.04×, 20.9×, 357×, 35,800×."""
    n = float(f"{n:.3g}")
    return f"{n:,.0f}×" if n >= 100 else f"{n:.1f}×" if n >= 10 else f"{n:.2f}×"


def speed_text(ratio: float, verdict: str | None = None) -> str:
    """The headline form of ours ÷ theirs (a time ratio): how many times faster, or slower, we are than the
    strongest competitor. Higher is better, and a loss is stated as a loss."""
    if abs(ratio - 1) < 0.005:
        return "1.00× (level)"
    text = f"{fmt_factor(1 / ratio)} faster" if ratio < 1 else f"{fmt_factor(ratio)} slower"
    # A gap inside the kit's noise floor is level, whichever way the means fall.
    return f"level ({text})" if verdict == "tie" else text


def hero_name(a: dict) -> str:
    return HERO_LABEL.get(a["task"], task_name(a))


def chart_alt(ctx_library: str, aggs: list[dict]) -> str:
    """Alt text for the chart-only block, where no table follows the chart."""
    return hero_alt(ctx_library, aggs).replace("Bar chart of the table below:", "Bar chart:", 1)


def hero_alt(ctx_library: str, aggs: list[dict]) -> str:
    """Short alt text: the table follows the chart and carries every number, which the chart's description
    repeats for readers who open the image on its own."""
    n = {k: sum(1 for a in aggs if a["verdict"] == k) for k in ("win", "tie", "mixed", "loss")}
    none = sum(1 for a in aggs if a["ratio"] is None)
    return (f"Bar chart of the table below: how many times faster {ctx_library} is than the strongest competitor "
            f"on each task, log scale, higher is better. {len(aggs)} tasks: {n['win']} better, {n['tie']} level, "
            f"{n['mixed']} mixed, {n['loss']} worse, {none} not comparable.")


def hero_desc(title: str, note: str, aggs: list[dict]) -> str:
    """The chart's full description, with every task's number or reason."""
    parts = [f"{title}. {note}"]
    items = []
    for a in aggs:
        if a["ratio"] is None:
            items.append(f"{hero_name(a)} {chart_tag(a)}")
            continue
        t = a["tally"]
        used = len(a["t_own"])
        items.append(f"{hero_name(a)} {speed_text(a['ratio'], a['verdict'])} {MARK[a['verdict']].split(' ', 1)[1]} "
                     f"(ours {report.fmt_time(a['own_time'])} vs {report.fmt_time(a['best_time'])}; workloads: "
                     f"{t['win']} better, {t['tie']} level, {t['loss']} worse of {used}"
                     + ("; partial, no number for " + ", ".join(f"{w} ({s})" for w, s in a["missing"]) if a["missing"] else "")
                     + ")")
    parts.append("; ".join(items) + ".")
    return " ".join(parts)


HERO_NOTE = ("Times faster than the strongest competitor on each task (geometric mean over its workloads), log scale: "
             "bars to the right are faster, higher is better, bars to the left are slower.")


def chart_files(ctx: Ctx, aggs: list[dict], rel: str, short_alt: bool = False) -> list[str]:
    # Every task has a row; one with no number is a labelled row without a bar.
    data = [{"workload": HERO_CHART_LABEL.get(a["task"], task_name(a)), "library": ctx.library,
             "vs": " / ".join(sorted(a["vs"])) or "—",
             "verdict": a["verdict"] or "tie", "ratio": a["ratio"], "tag": chart_tag(a),
             "own": a.get("own_time"), "rival": a.get("best_time")} for a in aggs]
    if not data:
        return []
    name = f"{ctx.library}-hero"
    title = f"{ctx.library}: speed per task against the strongest competitor"
    # Only an explicit omission drops the sentence, so a chart without the option reads as before.
    marks = HERO_DESC_DAGGER if "unchanged-emit" in ctx.chart_omit else HERO_DESC_MARKS
    desc = hero_desc(title, HERO_NOTE + " " + marks, aggs)
    alt = hero_alt(ctx.library, aggs)
    for theme in report.THEMES:
        ctx.files[f"{name}-{theme}.svg"] = report.svg_chart(title, HERO_NOTE, data, theme, report.fmt_time, alt,
                                                            desc=desc, invert=True, label_of=speed_label)
    # The SVG keeps the full description either way; only the picture's alt text differs.
    return [report.picture(name, chart_alt(ctx.library, aggs) if short_alt else alt, rel), ""]


def speed_label(ratio: float) -> str:
    return speed_text(ratio)


HERO_DESC_DAGGER = "A † marks a cell measured differently from the task as specified."
# The sentence about unchanged emit is left out when that task is left out of the chart.
HERO_DESC_MARKS = ("Unchanged emit times the write path of a document that was not edited; a library that keeps the "
                   "text returns it without rendering, so it is not a rendering speed. " + HERO_DESC_DAGGER)


# ---- the blocks -------------------------------------------------------------------------

def chart_aggs(ctx: Ctx) -> list[dict]:
    """The per-task entries of the hero and chart blocks: every task except those left out."""
    return [a for a in aggregate(ctx.result, ctx.library, ctx.rows) if a["task"] not in ctx.chart_omit]


def render_hero(ctx: Ctx, chart_rel: str, **_) -> str:
    """Time only, one row per task: the headline is how many times faster we are than the strongest
    competitor (their time ÷ ours, higher is better; a loss reads "slower"). Memory and the counts of
    better, level and worse workloads are on the full page."""
    aggs = chart_aggs(ctx)
    out = smoke_notice(ctx) + chart_files(ctx, aggs, chart_rel)
    body = []
    covers = []
    for a in aggs:
        cover = []
        if a["missing"]:
            cover.append("no number: " + ", ".join(f"`{w}` ({s})" for w, s in a["missing"]))
        if a["no_rival"]:
            cover.append("no competitor answer: " + ", ".join(f"`{w}`" for w in a["no_rival"]))
        if cover:
            covers.append(f"{hero_name(a)}: " + "; ".join(cover))
        if a["ratio"] is None:
            body.append([hero_name(a), "—", "—", "—", "—", "NOT COMPARABLE" if a["missing"] else "no rival"])
            continue
        names = lambda d: ", ".join(  # noqa: E731
            f"`{ctx.version_of(k)}`" + (f" ({v} of {sum(d.values())})" if len(d) > 1 else "")
            for k, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0])))
        vs = names(a["vs"])
        if a["marks"]:
            vs += f"{report.MARK_NOT_BEST} ({'; '.join(sorted(a['marks']))})"
        mine = report.fmt_time(a["own_time"])
        if a["own_marks"]:
            mine += f"{report.MARK_NOT_BEST} ({'; '.join(sorted(a['own_marks']))})"
        mark = verdict_text(a["verdict"]) + (" (partial: a workload has no number)" if a["missing"] else "")
        body.append([hero_name(a), mine, vs, report.fmt_time(a["best_time"]), speed_text(a["ratio"], a["verdict"]), mark])
    not_run = [r for r in ctx.not_run if r["task"] not in ctx.chart_omit]
    for task in task_sort({r["task"] for r in not_run} - {a["task"] for a in aggs}):
        body.append([HERO_LABEL.get(task, TASK_LABEL.get(task, task)), "—", "—", "—", "—", "NOT RUN"])
    kinds = {a["basis"] for a in aggs}
    out.append(report.md_table(
        ["Task", f"`{ctx.library}` time (geometric mean)", "Strongest competitor",
         "Its time (geometric mean)", "Speed against it (its time ÷ ours; higher is better)", "Verdict"], body))
    out.append("")
    out += not_run_text(ctx, not_run)
    if covers:
        out.append("Not in a row's mean: " + "; ".join(covers) + ".")
        out.append("")
    basis = {a["task"]: a for a in aggs}
    nrw = sorted({TASK_LABEL.get(t, t) for t, a in basis.items() if a["basis"] != "real-world"})
    out.append("Each row is the geometric mean, over the " + ("real-world workloads" if kinds <= {"real-world"} else
               "real-world workloads (all workloads for " + ", ".join(nrw) + ", which have no real-world one)")
               + " the task has, of our time and of the time of the strongest competitor in each workload. "
               "Speed is the competitor's time divided by ours: 2× faster means half the time, and a loss is "
               "stated as slower. Where several competitors were strongest the column names them all, with how "
               "often. A † marks a cell measured differently from the task as specified; the note beside each mark "
               "says how. A marked competitor still counts. A workload where our library has no number is listed "
               "and the row is marked as partial, never counted as a win. The verdict is never better than the "
               "per-workload verdicts allow: a row with a losing workload is marked mixed or loss, and a row with "
               "no winning workload is never marked a win. Memory and the count of better, level and worse "
               "workloads in each row are on the full page.")
    out.append("")
    out += method_strip(ctx)
    return "\n".join(out)


def render_chart(ctx: Ctx, chart_rel: str, performance_link: str = "docs/performance.md", **_) -> str:
    """The hero chart alone, with a link to the full results: for a README that keeps the numbers on a
    page of their own."""
    aggs = chart_aggs(ctx)
    out = smoke_notice(ctx) + chart_files(ctx, aggs, chart_rel, short_alt=True)
    out.append(f"[Full results, with memory and the method →]({performance_link})")
    return "\n".join(out)


def render_full(ctx: Ctx, **_) -> str:
    libs = [ctx.library] + [l["id"] for l in ctx.libs() if l["id"] != ctx.library]
    present = {c["library"] for c in ctx.cells}
    libs = [l for l in libs if l in present]
    wl = {w["id"]: w for w in ctx.result["workloads"]}
    out = smoke_notice(ctx)
    notes = reasons.Notes([c for r in ctx.rows for c in r["cells"]])  # the cells the tables show
    for metric, title, cellfn in (("time", "Time per operation", report.time_cell),
                                  ("memory", report.memory_label(ctx.lang), report.memory_cell)):
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
                        + ["—" if l not in by else ("unsupported" if by[l]["status"] == "unsupported" else cellfn(by[l], notes))
                           for l in libs] + [rel])
        out.append(f"**{title}**\n")
        out.append(report.md_table(["Task", "Workload", "Input"] + [f"`{l}`" for l in libs]
                                   + ["Ours vs strongest competitor"], body))
        out.append("")
    out += not_run_text(ctx)
    if notes.groups:
        out += ["Cells without a number, with what the correctness guard found:", ""]
        out += notes.lines()
        out.append("")
    out.append("Relative figures are ours ÷ the strongest competitor in that row, so below 1.0× is better for us "
               "and ▼ marks a loss. A † marks a cell measured differently from the task as specified; the note "
               "beside each mark says how. Cells without a number say why.")
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


KINDS = {"bench-hero": render_hero, "bench-chart": render_chart, "conformance-line": render_conformance_line,
         "bench-full": render_full, "conformance-deviations": render_deviations}


# ---- update -----------------------------------------------------------------------------

BRANCH_NAMES = {"main", "master", "head", "trunk", "develop", "dev", "latest"}


def branch_ref(url: str) -> str | None:
    """A path segment of `url` that names a branch (`main`, `refs/heads/...`), or None. A branch moves, so
    charts read from it stop matching the numbers beside them once the next run lands."""
    segs = [x for x in url.split("://", 1)[1].split("/")[1:] if x]
    for i, seg in enumerate(segs):
        # The first two segments are the owner and the repository, whatever they are called.
        if i >= 2 and seg.lower() in BRANCH_NAMES or (seg == "refs" and segs[i + 1:i + 2] == ["heads"]):
            return seg if seg != "refs" else "refs/heads"
    return None


RAW_BASE = re.compile(r"https://raw\.githubusercontent\.com/([^/\s]+)/([^/\s]+)/([^/\s]+)/?$")


def performance_url(chart_base: str | None, perf_rel: str, repo_path: str) -> str:
    """The link to the performance page: relative to the file, or, for a raw.githubusercontent.com base
    pinned to a ref, the github.com page of the same ref (a relative link does not resolve on a package
    page, and the raw URL would show the source). Any other base keeps the relative link."""
    m = chart_base and RAW_BASE.match(chart_base)
    if not m:
        return perf_rel
    return f"https://github.com/{m[1]}/{m[2]}/blob/{m[3]}/{repo_path}"


def update(ctx: Ctx, targets: list[Path], charts: str, docs_conformance: Path,
           chart_base: str | None = None) -> dict[Path, bytes]:
    """The new bytes of every target file that holds blocks; nothing is written here. A target with no
    markers is skipped only when it is not the first (the README); the first must have at least one."""
    known = set(KINDS)
    chart_abs = targets[0].parent / charts
    if chart_base is not None and not re.match(r"https?://[^\s]+$", chart_base):
        raise ReadmeError(f"the chart base must be an http or https URL, not {chart_base!r}")
    if chart_base is not None and (ref := branch_ref(chart_base)):
        raise ReadmeError(f"the chart base names the branch {ref!r}, whose charts change after a release: "
                          "pin it to a tag, or leave it out for links relative to the file")
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
        # With a base URL the chart links are absolute (the README may be shown where relative paths do not
        # resolve, such as a package registry page); the same path under the base for every target file.
        chart_rel = (chart_base.rstrip("/") + "/" + Path(charts).as_posix().strip("/")
                     if chart_base else rel(chart_abs))
        perf = docs_conformance.parent / "performance.md"
        perf_link = performance_url(chart_base, rel(perf),
                                    Path(os.path.relpath(perf, targets[0].parent)).as_posix())
        blocks = {k: KINDS[k](ctx, chart_rel=chart_rel, conformance_link=rel(docs_conformance),
                              performance_link=perf_link)
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
