"""Production-name report: which YAML 1.2.2 productions the kit's own cases name.

This counts names, not coverage. A case lists the productions its author says it exercises;
nothing checks that the case exercises every alternative of a production, or exercises it
well. Some names are derived, not chosen: a case that uses a construct always uses the
indicator characters in it (`#` for a comment, `&` for an anchor), so those productions are
added from the construct and reported apart from the ones an author wrote down.

The vendored yaml-test-suite carries no production mapping, so it is counted in the corpus
size and not here. A production no case names is a gap to fill, not a failure.
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

# A construct always carries these productions: its indicator characters and the parts every
# instance of it has. Applied when the report is built, never written into a case.
DERIVED = {
    "75": ["12"],            # comment text: the # indicator
    "101": ["13"],           # anchor property: the & indicator
    "104": ["14"],           # alias node: the * indicator
    "97": ["15"],            # tag property: the ! indicator
    "98": ["15"], "99": ["15"],
    "170": ["16"],           # literal scalar: the | indicator
    "174": ["17"],           # folded scalar: the > indicator
    "120": ["18"],           # single-quoted scalar: the ' indicator
    "109": ["19"],           # double-quoted scalar: the " indicator
    "82": ["20"],            # directive: the % indicator
    "183": ["4", "63", "184"],   # block sequence: the - indicator, indentation, its entries
    "188": ["5"],            # block mapping entry written with ?
    "187": ["6", "63", "188"],   # block mapping: the : indicator, indentation, its entries
    "137": ["8", "9"],       # flow sequence: [ and ]
    "140": ["10", "11"],     # flow mapping: { and }
}

# Every error category the kit can expect (see docs/case-format.md).
CATEGORIES = ("syntax", "encoding", "limit", "patch-path", "patch-test", "patch-invalid",
              "refused-alias", "refused-ambiguous", "unresolved-tag")


def _check(c, p: str) -> int:
    if not p.isdigit() or not 1 <= int(p) <= TOTAL:
        raise ValueError(f"{c.id}: {p!r} is not a production number from 1 to {TOTAL}")
    return int(p)


def production_numbers(cases) -> dict:
    """Production number -> sorted ids of the cases that name it in their own list."""
    out: dict[int, list] = {}
    for c in cases:
        for p in c.productions:
            out.setdefault(_check(c, p), []).append(c.id)
    return {k: sorted(set(v)) for k, v in sorted(out.items())}


def derived_numbers(cases) -> dict:
    """Production number -> sorted ids of the cases that carry it only through DERIVED."""
    out: dict[int, list] = {}
    for c in cases:
        own = {_check(c, p) for p in c.productions}
        for p in c.productions:
            for d in DERIVED.get(p, []):
                if int(d) not in own:
                    out.setdefault(int(d), []).append(c.id)
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
    named = production_numbers(own)
    derived = derived_numbers(own)
    by_hand = set(named)
    only_derived = set(derived) - by_hand
    lines = ["# Production names", "",
             "Generated from the `productions` field of the kit's own cases. This counts the "
             "productions that cases name, not how well they are tested: a name is the case "
             "author's claim, and nothing checks that a case exercises every alternative of a "
             "production. The vendored yaml-test-suite is not mapped to productions and is not "
             "counted here.", ""]
    n_with = sum(1 for c in own if c.productions)
    lines += ["## Headline", "",
              f"- Productions named in at least one case's `productions` list: "
              f"**{len(by_hand)} of {TOTAL}**",
              f"- Productions not named by hand but implied by a construct a case uses "
              f"(indicator characters, indentation, entries): {len(only_derived)} more, "
              f"**{len(by_hand | only_derived)} of {TOTAL}** in all",
              f"- Own cases: {len(own)}, of which {n_with} name at least one production",
              ""]
    lines += ["## By chapter", "",
              "| Chapter | Productions | Named by hand | Implied only | Not named by any case |",
              "|---|---|---|---|---|"]
    for name, (lo, hi) in CHAPTERS.items():
        nums = list(range(lo, hi + 1))
        hand = [n for n in nums if n in by_hand]
        imp = [n for n in nums if n in only_derived]
        missing = [n for n in nums if n not in by_hand and n not in only_derived]
        lines.append(f"| {name} | {lo}-{hi} ({len(nums)}) | {len(hand)} | {len(imp)} | "
                     f"{_ranges(missing) or 'none'} |")
    lines += ["", "## Cases per named production", "",
              "| Production | Cases that name it | Cases that imply it |", "|---|---|---|"]
    for k in sorted(by_hand | only_derived):
        lines.append(f"| {k} | {len(named.get(k, []))} | {len(derived.get(k, []))} |")
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
    jt = Counter(t for c in own for t in c.tags if t in ("convention", "comment-ownership"))
    if jt:
        lines += ["## Cases that rest on a convention", "",
                  "Tagged so that a library is not failed on taste unnoticed.", ""]
        lines += [f"- `{t}`: {n}" for t, n in sorted(jt.items())]
        lines.append("")
    return "\n".join(lines)
