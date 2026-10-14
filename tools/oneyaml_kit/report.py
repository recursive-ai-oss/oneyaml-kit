"""Conformance report: Markdown generated from result files, never written by hand."""

from __future__ import annotations

from collections import OrderedDict

from . import compare

CRITERIA = """\
## How to read this

- **Loose**: the outcome only. A valid input must give exactly the expected result
  (the whole event stream, the whole value, every output byte). An invalid input
  must be reported as an error; its kind and position are not checked.
- The loose score on the yaml-test-suite events is the criterion most projects
  publish: a valid case gives exactly the recorded events, an invalid case gives an
  error.
- **Strict**: everything the expectation states. For invalid input that adds the
  error category and position where the case states them, the events emitted before
  the error (for the yaml-test-suite: the recorded `test.event` prefix), and, for a
  refused patch, that the document is unchanged.
- **Unsupported**: the adapter does not declare a capability the case needs, or it
  declined that case. It is not a pass, and it is counted
  separately. **Strict only** counts cases whose loose verdict was reached but whose
  strict check needs a capability (error category or position) the adapter does not
  declare. A pass count is always out of all cases in the group.
- **Emulated**: passes that depended on behaviour the adapter supplied itself because
  the library has none (for example discarding a half-applied patch). They are
  counted in Loose and Strict, and counted again here, so that adapter behaviour is
  never read as library behaviour. A library with a native implementation has none.
- **Convention cases**: a case tagged `convention` has exact bytes that rest on a rule the
  kit chose where the specification leaves a choice (for example where a new mapping key
  goes). Each adapter's section counts them apart, so a loss on one of them can be read as
  a difference of taste rather than of correctness.
- **Strict, corrected**: strict, judged against the kit's corrected expectation for
  the cases where the upstream one is wrong. Corrections and their reasons are in
  `suites/yaml-test-suite/corrections.json`.
- A listed deviation is a failure the library is known to have. It still counts as
  a failure; the list only guarantees that nothing fails unnoticed and that no
  entry goes stale.
"""


def group_of(r: dict) -> str:
    suite = "yaml-test-suite" if r["suite"] == "yaml-test-suite" else "kit cases"
    if r["suite"] == "yaml-test-suite":
        kind = "invalid input" if r["expect"] == "error" else "valid input"
        what = {"events": "events", "value": "values (in.json)"}[r["op"]]
        return f"{suite} · {what} · {kind}"
    what = {"events": "events", "value": "values", "patch": "edits by patch"}[r["op"]]
    return f"{suite} · {r['category']} · {what}"


def _count(rows: list[dict], mode: str, src=None) -> int:
    n = 0
    for r in rows:
        v = r if src is None else r.get("corrected", r)
        n += v[mode] == compare.PASS
    return n


CONVENTION = "convention"


def _convention_section(cases: list[dict]) -> list[str]:
    """Verdicts on cases whose exact bytes rest on a kit convention, per group.

    The main table counts them with everything else; this one lets a reader subtract them,
    so a library is never failed on taste unnoticed.
    """
    rows = [r for r in cases if CONVENTION in r.get("tags", [])]
    if not rows:
        return []
    groups: "OrderedDict[str, list]" = OrderedDict()
    for r in sorted(rows, key=lambda r: (group_of(r), r["id"])):
        groups.setdefault(group_of(r), []).append(r)
    out = ["Cases whose expected bytes rest on a kit convention (the specification does not decide "
           "them; see `docs/edit-semantics.md`). They are counted in the table above too.", "",
           "| Group | Cases | Loose pass | Loose fail | Strict pass | Strict fail | Unsupported |",
           "|---|---:|---:|---:|---:|---:|---:|"]

    def line(name, rs):
        c = lambda m, v: sum(1 for r in rs if r[m] == v)
        unsup = sum(1 for r in rs if r["loose"] == compare.UNSUPPORTED)
        return (f"| {name} | {len(rs)} | {c('loose', compare.PASS)} | {c('loose', compare.FAIL)} "
                f"| {c('strict', compare.PASS)} | {c('strict', compare.FAIL)} | {unsup} |")

    for name, rs in groups.items():
        out.append(line(name, rs))
    if len(groups) > 1:
        out.append(line("**all**", rows))
    out.append("")
    return out


def render_one(res: dict) -> str:
    a, k, run = res["adapter"], res["kit"], res["run"]
    lines = []
    lib = f"{a['name']} {a['version']}"
    lines.append(f"## {lib}")
    lines.append("")
    lines.append(f"Language: {a['language']}. Run date: {run['date']}. "
                 f"{run['cases']} cases in {run['seconds']} s ({run['mode']} mode"
                 f"{'' if run['complete'] else ', partial selection'}).")
    lines.append("")
    if a.get("emulates"):
        lines.append("Behaviours this adapter supplies itself because the library has none: "
                     + ", ".join(f"`{e}`" for e in a["emulates"]) + ". See the Emulated column.")
        lines.append("")
    groups: "OrderedDict[str, list]" = OrderedDict()
    for r in sorted(res["cases"], key=lambda r: (r["suite"] != "yaml-test-suite", group_of(r), r["id"])):
        groups.setdefault(group_of(r), []).append(r)
    lines.append("| Group | Cases | Loose | Strict | Strict, corrected | Unsupported | Strict only | Emulated |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")

    def row(name: str, rows: list[dict]) -> str:
        n = len(rows)
        unsup = sum(1 for r in rows if r["strict"] == compare.UNSUPPORTED and r["loose"] == compare.UNSUPPORTED)
        corrected = "–"
        if any("corrected" in r for r in rows):
            corrected = f"{_count(rows, 'strict', True)}/{n}"
        strict_only = sum(1 for r in rows if r["strict"] == compare.UNSUPPORTED and r["loose"] != compare.UNSUPPORTED)
        emulated = sum(1 for r in rows if r.get("emulated") and compare.PASS in (r["loose"], r["strict"]))
        return (f"| {name} | {n} | {_count(rows, 'loose')}/{n} | {_count(rows, 'strict')}/{n} "
                f"| {corrected} | {unsup} | {strict_only} | {emulated} |")

    for name, rows in groups.items():
        lines.append(row(name, rows))
    yts = [r for r in res["cases"] if r["suite"] == "yaml-test-suite" and r["op"] == "events"]
    if yts:
        lines.append(row("**yaml-test-suite · events · all 402**" if len(yts) == 402
                         else "**yaml-test-suite · events · all**", yts))
    lines.append("")
    lines += _convention_section(res["cases"])
    d = res["deviations"]
    entries = d.get("entries", [])
    lines.append(f"Known deviations listed: {len(entries)}. "
                 f"Unlisted failures: {len(d['unlisted'])}. Stale entries: {len(d['stale']) + len(d['unknown'])}.")
    lines.append("")
    if entries:
        lines.append("| Case | Expectation | Mode | Verdict | Reason |")
        lines.append("|---|---|---|---|---|")
        for e in sorted(entries, key=lambda e: (e["case"], e["expectation"], e["mode"])):
            lines.append(f"| `{e['case']}` | {e['expectation']} | {e['mode']} | {e['verdict']} "
                         f"| {e['reason'].replace('|', '/')} |")
        lines.append("")
    problems = d["unlisted"] + d["stale"] + d["unknown"]
    if problems:
        lines.append("Unresolved:")
        lines.append("")
        for p in problems[:50]:
            lines.append(f"- {p.replace('|', '/')}")
        lines.append("")
    return "\n".join(lines)


def render_skipped(skipped) -> str:
    """The adapters that did not run here and why, so a missing row is never silent."""
    lines = ["## Not run", "", "These adapters were skipped because this machine lacks something "
             "they need. They are not failures and have no results below.", ""]
    lines += [f"- `{n}`: {why}" for n, why in skipped]
    return "\n".join(lines) + "\n"


def render(results: list[dict], skipped=()) -> str:
    if not results and not skipped:
        raise ValueError("no results")
    if not results:
        return "# Conformance report\n\n" + render_skipped(skipped)
    k = results[0]["kit"]
    ys = k["suites"]["yaml-test-suite"]
    head = [
        "# Conformance report",
        "",
        f"Kit version {k['version']} (corpus digest `{k['corpus_digest'][:12]}`). "
        f"Scored baseline: yaml-test-suite `{ys['release']}` (revision `{ys['revision'][:8]}`), "
        f"plus {k['suites']['own']['cases']} kit cases. Generated by `bin/kit report`; do not edit.",
        "",
        CRITERIA,
    ]
    body = [render_one(r) for r in sorted(results, key=lambda r: r["adapter"]["name"])]
    md = "\n".join(head) + "\n" + "\n".join(body).rstrip() + "\n"
    return md + "\n" + render_skipped(skipped) if skipped else md
