"""Spec-coverage report: which YAML 1.2.2 productions and error paths the kit's own cases
exercise, generated from the cases' `productions` fields and expectations.

The vendored yaml-test-suite carries no production mapping, so it is counted in the corpus
size and not in the coverage. A production with no case is a gap to fill, not a failure.
"""

from __future__ import annotations

from collections import Counter, OrderedDict

# The 211 productions of YAML 1.2.2 by chapter of the specification.
CHAPTERS = OrderedDict([
    ("5 Character productions", (1, 62)),
    ("6 Structural productions", (63, 103)),
    ("7 Flow style productions", (104, 161)),
    ("8 Block style productions", (162, 201)),
    ("9 Document stream productions", (202, 211)),
])
TOTAL = 211

# Every error category the kit can expect (see docs/case-format.md).
CATEGORIES = ("syntax", "encoding", "limit", "patch-path", "patch-test", "patch-invalid",
              "refused-alias", "refused-ambiguous", "unresolved-tag")


def production_numbers(cases) -> dict:
    """Production number -> sorted ids of the own cases that name it."""
    out: dict[int, list] = {}
    for c in cases:
        for p in c.productions:
            if not p.isdigit() or not 1 <= int(p) <= TOTAL:
                raise ValueError(f"{c.id}: {p!r} is not a production number from 1 to {TOTAL}")
            out.setdefault(int(p), []).append(c.id)
    return {k: sorted(set(v)) for k, v in sorted(out.items())}


def error_categories(cases) -> Counter:
    """Error categories that cases expect, counting each case once per category."""
    n: Counter = Counter()
    for c in cases:
        e = c.expect
        cats = []
        if e["kind"] == "error":
            cats = [e["category"]]
        elif e["kind"] == "bounded" and e.get("category") is not None:
            cat = e["category"]
            cats = list(cat) if isinstance(cat, (list, tuple)) else [cat]
        for x in set(cats):
            n[x] += 1
    return n


def _ranges(nums: list) -> str:
    """[1, 2, 3, 7, 9, 10] -> '1-3, 7, 9-10'."""
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


def render(cases) -> str:
    own = [c for c in cases if c.suite == "own"]
    prods = production_numbers(own)
    covered = set(prods)
    lines = ["# Specification coverage", "",
             "Generated from the `productions` field of the kit's own cases. The vendored "
             "yaml-test-suite is not mapped to productions and is not counted here.", ""]
    n_with = sum(1 for c in own if c.productions)
    lines += ["## Headline", "",
              f"- Productions with at least one case: **{len(covered)} of {TOTAL}**",
              f"- Own cases: {len(own)}, of which {n_with} name at least one production",
              ""]
    lines += ["## By chapter", "", "| Chapter | Productions | With a case | Without a case |",
              "|---|---|---|---|"]
    for name, (lo, hi) in CHAPTERS.items():
        nums = list(range(lo, hi + 1))
        missing = [n for n in nums if n not in covered]
        lines.append(f"| {name} | {lo}-{hi} ({len(nums)}) | {len(nums) - len(missing)} | "
                     f"{_ranges(missing) or 'none'} |")
    lines += ["", "## Cases per covered production", "", "| Production | Cases |", "|---|---|"]
    lines += [f"| {k} | {len(v)} |" for k, v in prods.items()]
    lines += ["", "## Corpus by category and origin", "", "| Category | Cases | Origins |", "|---|---|---|"]
    cats: dict = {}
    for c in own:
        cats.setdefault(c.category, []).append(c)
    for k in sorted(cats):
        o = Counter(c.origin for c in cats[k])
        lines.append(f"| {k} | {len(cats[k])} | {', '.join(f'{a} {b}' for a, b in sorted(o.items()))} |")
    lines += ["", "## Error paths", "",
              "Cases that expect an error, by category. A category with no case is a gap.", "",
              "| Category | Cases |", "|---|---|"]
    ec = error_categories(own)
    for k in CATEGORIES:
        lines.append(f"| {k} | {ec.get(k, 0)} |")
    gaps = [k for k in CATEGORIES if not ec.get(k)]
    lines += ["", "Categories without a case: " + (", ".join(gaps) if gaps else "none"), ""]
    jt = Counter(t for c in own for t in c.tags if t in ("judgement", "comment-ownership"))
    if jt:
        lines += ["## Cases that rest on a convention", "",
                  "Tagged so that a library is not failed on taste unnoticed.", ""]
        lines += [f"- `{t}`: {n}" for t, n in sorted(jt.items())]
        lines.append("")
    return "\n".join(lines)
