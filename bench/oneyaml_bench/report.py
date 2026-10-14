"""Generates the benchmark report: Markdown tables and light/dark SVG bar charts.

Nothing here is hand-written output. Every number comes from a result file, every table
cell says whether it is a measurement or why it is not, and losses are marked where they
occur. Charts are zero-based, linear, and carry their numbers in the alt text.
"""

from __future__ import annotations

import html
import math
from pathlib import Path

from . import results

TASK_ABOUT = {
    "typed-load": "Parse into the language's own typed model (structs).",
    "generic-load": "Parse into the library's generic value tree; all documents of a stream.",
    "dump": "Write a generic value tree back to YAML text.",
    "document-parse": "Parse into a lossless document that keeps comments and layout.",
    "unchanged-emit": "Write a parsed lossless document back; the bytes must equal the input.",
    "edit-script": "Apply a fixed script of edits to a parsed document and write the bytes.",
    "patch": "Apply an RFC 6902 patch to a document, atomically, and write the bytes.",
}
TASK_ORDER = list(TASK_ABOUT)


# ---- formatting --------------------------------------------------------------------------

def fmt_time(ns: float) -> str:
    for unit, scale in (("s", 1e9), ("ms", 1e6), ("µs", 1e3)):
        if ns >= scale:
            return f"{ns / scale:.3g} {unit}"
    return f"{ns:.3g} ns"


def fmt_bytes(b: float) -> str:
    for unit, scale in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if b >= scale:
            return f"{b / scale:.3g} {unit}"
    return f"{int(b)} B"


def fmt_ratio(r: float) -> str:
    return f"{r:.2f}×" if r < 10 else f"{r:.1f}×"


def fmt_size(n: int | None) -> str:
    return "—" if n is None else fmt_bytes(n)


MARK = {"win": "▲ win", "tie": "● tie", "loss": "▼ loss"}


def time_cell(c: dict) -> str:
    if c["status"] != "ok":
        return {"differs": "differs", "rejects": "rejects", "unsupported": "—", "unjudged": "unjudged",
                "error": "error"}[c["status"]]
    t = c["time"]
    spread = results.rel_spread(t["median_ns"], t["mad_ns"]) * 100
    note = "" if c.get("variant") in (None, "spec") else f" ({c['variant']})"
    return f"{fmt_time(t['median_ns'])} ±{spread:.0f}%{note}"


def memory_cell(c: dict) -> str:
    if c["status"] != "ok":
        return time_cell(c)
    m = c["memory"]
    text = f"{fmt_bytes(m['peak_bytes'])} / {fmt_bytes(m['allocated_bytes'])}"
    # What the library keeps after the task (pools, caches), where the harness measures it.
    if m.get("retained_bytes") is not None:
        text += f" (keeps {fmt_bytes(m['retained_bytes'])})"
    return text


def md_table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def source_text(src: dict) -> str:
    """Short provenance for a table: repository, path and a short revision."""
    if "repository" in src:
        repo = src["repository"].replace("https://github.com/", "")
        return f"{repo} `{src['path']}` @ {src['revision'][:7]}"
    return src.get("generator", src.get("origin", ""))


# ---- data shaping ------------------------------------------------------------------------

def lib_order(cells: list[dict]) -> list[tuple[str, str, str]]:
    """(language, library, role) own first, then competitors by name."""
    seen = {(c["language"], c["library"], c["role"]) for c in cells}
    rank = {"own": 0, "competitor": 1, "control": 2}
    return sorted(seen, key=lambda x: (x[0], rank.get(x[2], 3), x[1]))


def workload_order(result: dict) -> dict[str, int]:
    return {w["id"]: i for i, w in enumerate(result["workloads"])}


def ratios(rows: list[dict], metric: str) -> list[dict]:
    """Chart rows. The ratio is ours ÷ best competitor, the same number the tables show, so
    below 1.0 is better for our library on both time and memory."""
    out = []
    for r in rows:
        for e in r["own"]:
            if e["status"] != "ok" or e[metric] is None:
                continue
            m = e[metric]
            best = r["best_time" if metric == "time" else "best_memory"]
            own = next(c for c in r["cells"] if c["library"] == e["library"])
            key = ("time", "median_ns") if metric == "time" else ("memory", "peak_bytes")
            out.append({
                "workload": r["workload"], "library": e["library"], "vs": m["vs"], "verdict": m["verdict"],
                "ratio": m["ratio"],
                "own": own[key[0]][key[1]], "rival": best[key[0]][key[1]],
            })
    return out


# ---- SVG charts --------------------------------------------------------------------------

THEMES = {
    "light": {"bg": "#ffffff", "fg": "#1f2328", "muted": "#59636e", "grid": "#d1d9e0",
              "win": "#1a7f37", "tie": "#6e7781", "loss": "#cf222e"},
    "dark": {"bg": "#0d1117", "fg": "#e6edf3", "muted": "#9198a1", "grid": "#3d444d",
             "win": "#3fb950", "tie": "#8b949e", "loss": "#ff7b72"},
}

# The axis shows ratios from 1/8 to 8 on a log scale around 1.0x (equal). On a linear axis a
# loss (a ratio above 1.0) would be drawn long and a win short, or the reverse, and either
# way one side would be squeezed; on a log axis twice as slow and twice as fast are equally
# long. Bars grow from the 1.0x line: left is better for us, right is worse.
AXIS_OCTAVES = 3
TICKS = [1 / 8, 1 / 4, 1 / 2, 1.0, 2.0, 4.0, 8.0]
CHART_NOTE = ("Ours ÷ best competitor, log scale: left of the 1.0× line is better for us, "
              "right of it is a loss.")


def tick_label(t: float) -> str:
    return f"{t:g}×" if t >= 1 else f"1/{round(1 / t)}×"


def chart_alt(title: str, unit_note: str, data: list[dict], fmt) -> str:
    parts = [f"{title}. {unit_note}"]
    wins = sum(1 for d in data if d["verdict"] == "win")
    losses = sum(1 for d in data if d["verdict"] == "loss")
    ties = len(data) - wins - losses
    parts.append(f"{len(data)} workloads: {wins} better, {ties} level, {losses} worse.")
    parts.append("; ".join(f"{d['workload']} {fmt_ratio(d['ratio'])} ({d['library']} {fmt(d['own'])} vs {d['vs']} {fmt(d['rival'])}"
                            f"{', loss' if d['verdict'] == 'loss' else ''})" for d in data) + ".")
    return " ".join(parts)


def svg_chart(title: str, unit_note: str, data: list[dict], theme: str, fmt, alt: str) -> str:
    c = THEMES[theme]
    row_h, left, right, top = 24, 230, 190, 64
    width = 860
    plot = width - left - right
    centre = left + plot / 2
    height = top + row_h * len(data) + 34

    def x(v: float) -> float:
        octaves = max(-AXIS_OCTAVES, min(AXIS_OCTAVES, math.log2(v)))
        return centre + octaves / AXIS_OCTAVES * (plot / 2)

    esc = html.escape
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
         f'role="img" aria-label="{esc(alt, quote=True)}">',
         f"<title>{esc(title)}</title><desc>{esc(alt)}</desc>",
         f'<rect width="{width}" height="{height}" fill="{c["bg"]}"/>',
         f'<g font-family="system-ui, -apple-system, Segoe UI, sans-serif" fill="{c["fg"]}">',
         f'<text x="16" y="24" font-size="15" font-weight="600">{esc(title)}</text>',
         f'<text x="16" y="44" font-size="12" fill="{c["muted"]}">{esc(unit_note)}</text>']
    for t in TICKS:
        dash = ' stroke-dasharray="4 3"' if t == 1.0 else ""
        o.append(f'<line x1="{x(t):.1f}" y1="{top - 6}" x2="{x(t):.1f}" y2="{top + row_h * len(data)}" '
                 f'stroke="{c["grid"]}" stroke-width="{2 if t == 1.0 else 1}"{dash}/>')
        o.append(f'<text x="{x(t):.1f}" y="{top + row_h * len(data) + 16}" font-size="11" text-anchor="middle" '
                 f'fill="{c["muted"]}">{tick_label(t)}</text>')
    for i, d in enumerate(data):
        y = top + i * row_h
        colour = c[d["verdict"]]
        r = d["ratio"]
        clipped = abs(math.log2(r)) > AXIS_OCTAVES
        x0, x1 = sorted((centre, x(r)))
        o.append(f'<text x="{left - 8}" y="{y + 15}" font-size="12" text-anchor="end">{esc(d["workload"])}</text>')
        o.append(f'<rect x="{x0:.1f}" y="{y + 3}" width="{max(x1 - x0, 2.0):.1f}" height="{row_h - 8}" fill="{colour}"/>')
        label = fmt_ratio(r) + (" ▸" if clipped and r > 1 else " ◂" if clipped else "") \
            + ("  ▼ loss" if d["verdict"] == "loss" else "")
        lx = x1 + 6 if r >= 1 else x0 - 6
        anchor = "start" if r >= 1 else "end"
        o.append(f'<text x="{lx:.1f}" y="{y + 15}" font-size="11" text-anchor="{anchor}">{esc(label)}</text>')
        side = d["note"] if "note" in d else f'{fmt(d["own"])} vs {fmt(d["rival"])}'
        o.append(f'<text x="{width - right + 90}" y="{y + 15}" font-size="10" fill="{c["muted"]}" text-anchor="start">'
                 f'{esc(side)}</text>')
    o.append("</g></svg>")
    return "\n".join(o) + "\n"


def picture(name: str, alt: str, rel: str) -> str:
    a = html.escape(alt, quote=True)
    return (f'<picture>\n  <source media="(prefers-color-scheme: dark)" srcset="{rel}/{name}-dark.svg">\n'
            f'  <img alt="{a}" src="{rel}/{name}-light.svg">\n</picture>')


# ---- languages without an own library ----------------------------------------------------

def own_languages(cells: list[dict]) -> set[str]:
    return {c["language"] for c in cells if c["role"] == "own"}


def baseline_stats(rows: list[dict], metric: str) -> list[dict]:
    """For a language with only competitors: each library's geometric mean of its value divided by the
    best library's in the same row (1.0 is the best in every row it has a number), over the rows where
    it has one, and the number of rows where it is the best."""
    key = ("time", "median_ns") if metric == "time" else ("memory", "peak_bytes")
    acc: dict[str, dict] = {}
    for r in rows:
        best = r["best_time" if metric == "time" else "best_memory"]
        if best is None:
            continue
        b = max(best[key[0]][key[1]], 1)
        for c in r["cells"]:
            if c["status"] != "ok" or c["role"] == "control":
                continue
            a = acc.setdefault(c["library"], {"logs": [], "best": 0})
            a["logs"].append(math.log(max(c[key[0]][key[1]], 1) / b))
            a["best"] += 1 if c["library"] == best["library"] else 0
    return [{"library": lib, "rows": len(a["logs"]), "best": a["best"],
             "ratio": math.exp(sum(a["logs"]) / len(a["logs"]))} for lib, a in sorted(acc.items())]


def baseline_chart_data(stats: list[dict]) -> list[dict]:
    return [{"workload": s["library"], "library": s["library"], "vs": "the best in each row", "verdict": "tie",
             "ratio": s["ratio"], "own": s["ratio"], "rival": 1.0,
             "note": f"{s['rows']} rows, best in {s['best']}"} for s in stats]


BASELINE_NOTE = ("Each library's value ÷ the best library's in the same row, geometric mean over its rows, "
                 "log scale: the line at 1.0× is the best library in every row.")


def baseline_alt(title: str, stats: list[dict]) -> str:
    return (f"{title}. {BASELINE_NOTE} " + "; ".join(
        f"{s['library']} {fmt_ratio(s['ratio'])} over {s['rows']} rows, best in {s['best']}" for s in stats) + ".")


# ---- the report --------------------------------------------------------------------------

def render(result: dict, out_dir: Path, chart_dir_name: str = "charts") -> str:
    """Writes the charts under out_dir/chart_dir_name and returns the Markdown."""
    results.validate(result)
    run = result["run"]
    env = run["environment"]
    pub = run["publishability"]
    cells = result["cells"]
    libs = lib_order(cells)
    rows_all = results.compare_rows(cells)
    wl_rank = workload_order(result)
    wl_by_id = {w["id"]: w for w in result["workloads"]}
    chart_dir = out_dir / chart_dir_name
    chart_dir.mkdir(parents=True, exist_ok=True)

    L = []
    label = "PUBLISHABLE" if pub["publishable"] else "SMOKE RESULT, NOT PUBLISHABLE"
    L.append("# YAML library benchmark results\n")
    L.append(f"**{label}.** {run['mode'].capitalize()} run on {run['date']}.\n")
    if pub["reasons"]:
        L.append("Why this run is not publishable:\n")
        L += [f"- {r}" for r in pub["reasons"]]
        L.append("")
    else:
        m = pub["measured"]
        L.append(f"The host was quiet: other work kept at most {m['foreign_busy_cores_p95']} cores busy for 95% of "
                 f"the run and {m['foreign_busy_cores_max']} at its worst; the load average was "
                 f"{m['load1_start']} before and {m['load1_end']} after.\n")

    L.append("## Environment\n")
    L.append(md_table(["", ""], [
        ["CPU", f"{env['cpu']} ({env['cpus']} logical CPUs)"],
        ["Memory", f"{env['memory_gb']} GiB"],
        ["OS", f"{env['os']} ({env['machine']})"],
        ["CPU governor, boost", f"{env['governor']}, {env['boost']}"],
        ["Pinned to CPU", "no" if env.get("pinned_cpu") is None else str(env["pinned_cpu"])],
        ["Host load (1 min)", f"{run['load']['before']} before, {run['load']['after']} after"],
        ["Foreign busy cores", f"p95 {pub['measured']['foreign_busy_cores_p95']}, max {pub['measured']['foreign_busy_cores_max']} "
                               f"({pub['measured']['load_samples']} samples)"],
        ["Run time", f"{run['seconds']} s, {run['restarts']} harness restarts"],
        ["Kit revision", str(env.get("kit_revision"))],
    ]))
    L.append("")
    for lang, h in sorted(result["harnesses"].items()):
        cfg = h["config"]
        L.append(f"**{lang} harness** `{h['name']} {h['version']}`, {h['runtime']}, target {h['target']}, "
                 f"flags {h['flags']}. Warm-up {cfg['warmup_ms']} ms, {cfg['samples']} samples of about "
                 f"{cfg['sample_target_ms']} ms; {h['timer']}. Memory: {h['memory']}.\n")
    L.append("## Libraries\n")
    lib_rows = []
    for lang, h in sorted(result["harnesses"].items()):
        for l in h["libraries"]:
            if l["role"] == "control":
                continue
            lib_rows.append([lang, f"`{l['id']}`", f"{l['package']} {l['version']}", l["role"],
                             ", ".join(f"{k}: {v}" for k, v in l["settings"].items()) or "defaults"])
    L.append(md_table(["Language", "Library", "Crate/package", "Role", "Settings"], lib_rows))
    L.append("")

    # Summary
    L.append("## Summary\n")
    L.append("Each row of the tables compares our library with the **best competitor in that row** "
             "(the fastest for time, the smallest peak for memory). A gap inside the measurement noise "
             "(at least 2% for time, 1% for memory) is a tie. Cells without a number say why.\n")
    summ = []
    own_langs = own_languages(cells)
    base_langs = sorted({c["language"] for c in cells} - own_langs)
    for lang in sorted(own_langs):
        lang_rows = [r for r in rows_all if r["cells"][0]["language"] == lang]
        for task in TASK_ORDER:
            tr = [r for r in lang_rows if r["task"] == task]
            if not tr:
                continue
            tt, tm = results.tally(tr, "time"), results.tally(tr, "memory")
            sp = ratios(tr, "time")
            worst = max(sp, key=lambda d: d["ratio"]) if sp else None
            summ.append([lang, task, str(len(tr)),
                         f"{tt['win']} / {tt['tie']} / {tt['loss']}", f"{tm['win']} / {tm['tie']} / {tm['loss']}",
                         str(tt["no-number"]), str(tt["no-rival"]),
                         "—" if worst is None else f"{worst['workload']} {fmt_ratio(worst['ratio'])} of {worst['vs']}"])
    if summ:
        L.append(md_table(["Language", "Task", "Workloads", "Time win / tie / loss", "Memory win / tie / loss",
                           "No number", "No rival", "Weakest row (our time ÷ best competitor's)"], summ))
        L.append("")
        L.append("*No number*: our library's answer was wrong, refused or missing. *No rival*: no competitor "
                 "produced a correct answer for that row, so there is nothing to compare with.\n")
    if base_langs:
        L.append("**Languages without a library of ours** (" + ", ".join(base_langs) + "): the competitors are "
                 "measured against each other, the baseline a library of ours has to beat. For each task, a "
                 "library's ratio is its value ÷ the best library's in the same row (1.0× is the best), as a "
                 "geometric mean over the rows where it has a number.\n")
        bsum = []
        for lang in base_langs:
            lang_rows = [r for r in rows_all if r["cells"][0]["language"] == lang]
            for task in TASK_ORDER:
                tr = [r for r in lang_rows if r["task"] == task]
                st, sm = baseline_stats(tr, "time"), baseline_stats(tr, "memory")
                if not tr or not st:
                    continue
                mem = {x["library"]: x for x in sm}
                for x in st:
                    m = mem.get(x["library"])
                    bsum.append([lang, task, f"`{x['library']}`", str(x["rows"]), str(x["best"]),
                                 fmt_ratio(x["ratio"]), "—" if m is None else str(m["best"]),
                                 "—" if m is None else fmt_ratio(m["ratio"])])
        L.append(md_table(["Language", "Task", "Library", "Rows with a number", "Fastest in", "Time ratio",
                           "Smallest peak in", "Peak ratio"], bsum))
        L.append("")

    L.append("A time in parentheses names the accepted variant of the expected result when it is not the "
             "plain one: *merged* means the library applied `<<` merge keys (a YAML 1.1 extension; YAML 1.2 "
             "reads `<<` as an ordinary key), and *bytes-only* means the library gives no value to compare, "
             "so its document is judged on the bytes it writes back. Both are listed as known deviations.\n")
    if any(c.get("variant") == "value-only" for c in cells):
        L.append("*value-only* means the library cannot keep the text of a document it patches (it works on "
                 "JSON, say), so the text it wrote is judged on the value it holds, not byte for byte. It is a "
                 "baseline for the cost of patching without keeping the text, and is listed as a known deviation.\n")

    # Per task sections
    for task in TASK_ORDER:
        task_rows = [r for r in rows_all if r["task"] == task]
        if not task_rows:
            continue
        L.append(f"## {task}\n")
        L.append(TASK_ABOUT[task] + "\n")
        for lang in sorted({c["language"] for c in cells}):
            lang_rows = sorted([r for r in task_rows if r["cells"][0]["language"] == lang],
                               key=lambda r: wl_rank.get(r["workload"], 0))
            lib_cols = [l for l in libs if l[0] == lang and l[2] != "control"]
            if not lang_rows:
                continue
            L.append(f"### {task}, {lang}\n")
            has_own = lang in own_langs
            for metric, title in (("time", "Time per operation"), ("memory", "Peak / allocated heap per operation")):
                header = ["Workload", "Input"] + [f"`{l[1]}`" for l in lib_cols] + \
                    ["Ours vs best competitor" if has_own else "Best in the row"]
                body = []
                for r in lang_rows:
                    by = {c["library"]: c for c in r["cells"]}
                    row = [f"`{r['workload']}`", fmt_size(wl_by_id[r["workload"]]["input_bytes"])]
                    for l in lib_cols:
                        c = by.get(l[1])
                        row.append("—" if c is None else (time_cell(c) if metric == "time" else memory_cell(c)))
                    verdicts = []
                    if not has_own:
                        best = r["best_time" if metric == "time" else "best_memory"]
                        verdicts.append("—" if best is None else f"`{best['library']}`")
                    for e in r["own"]:
                        if e["status"] != "ok":
                            verdicts.append(f"no number ({e['status']})")
                        elif e[metric] is None:
                            verdicts.append("no rival")
                        else:
                            m = e[metric]
                            verdicts.append(f"{fmt_ratio(m['ratio'])} of `{m['vs']}` {MARK[m['verdict']]}")
                    row.append("<br>".join(verdicts) or "—")
                    body.append(row)
                L.append(f"**{title}**\n")
                L.append(md_table(header, body))
                L.append("")
            for metric, fmt, unit in (("time", fmt_time, "time"), ("memory", fmt_bytes, "peak heap")):
                if not has_own:
                    stats = baseline_stats(lang_rows, metric)
                    if not stats:
                        continue
                    name = f"{lang}-{task}-{metric}"
                    title = f"{task} ({lang}): {unit}, each library against the best in the row"
                    alt = baseline_alt(title, stats)
                    data = baseline_chart_data(stats)
                    for theme in THEMES:
                        (chart_dir / f"{name}-{theme}.svg").write_text(
                            svg_chart(title, BASELINE_NOTE, data, theme, fmt, alt), encoding="utf-8")
                    L.append(picture(name, alt, chart_dir_name))
                    L.append("")
                    continue
                data = ratios(lang_rows, metric)
                if not data:
                    continue
                name = f"{lang}-{task}-{metric}"
                note = f"{unit.capitalize()}. {CHART_NOTE}"
                title = f"{task} ({lang}): {unit}, ours against the best competitor"
                alt = chart_alt(title, note, data, fmt)
                for theme in THEMES:
                    (chart_dir / f"{name}-{theme}.svg").write_text(svg_chart(title, note, data, theme, fmt, alt), encoding="utf-8")
                L.append(picture(name, alt, chart_dir_name))
                L.append("")

    # Cells without a number
    bad = [c for c in cells if c["status"] in ("differs", "rejects", "unjudged", "error")
           and c["role"] != "control"]
    L.append("## Cells without a number\n")
    if not bad:
        L.append("None: every library that was asked produced a correct answer or has no such capability.\n")
    else:
        L.append("A library that gives a wrong answer, refuses a valid input or cannot be measured gets no time. "
                 "The reason is the first difference the guard found. *Unjudged* means the harness's two reference "
                 "parsers disagree about the library's output, so the harness could not decide; it is not a "
                 "verdict on the library.\n")
        L.append(md_table(["Language", "Library", "Task", "Workload", "Status", "Reason"],
                          [[c["language"], f"`{c['library']}`", c["task"], f"`{c['workload']}`", c["status"],
                            (c.get("detail") or "").replace("|", "\\|").replace("\n", " ")[:160]] for c in bad]))
        L.append("")
    unsup = {}
    for c in cells:
        if c["status"] == "unsupported" and c["role"] != "control":
            unsup.setdefault((c["language"], c["library"], c["task"]), c.get("detail", ""))
    if unsup:
        L.append("### Unsupported tasks\n")
        L.append(md_table(["Language", "Library", "Task", "Why"],
                          [[a, f"`{b}`", t, d] for (a, b, t), d in sorted(unsup.items())]))
        L.append("")

    L.append("## Workloads\n")
    L.append(md_table(["Workload", "Kind", "Input", "Documents", "Source", "Licence"],
                      [[f"`{w['id']}`", w["kind"], fmt_size(w["input_bytes"]), str(w["documents"]),
                        source_text(w["source"]), w["source"].get("licence", "")] for w in result["workloads"]]))
    L.append("")
    L.append("Method, task definitions and the correctness guard are described in docs/benchmarks.md.\n")
    return "\n".join(L)
