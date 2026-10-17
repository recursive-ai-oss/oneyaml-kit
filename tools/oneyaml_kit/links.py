"""Check that relative links in the documentation point at files and headings that exist.

Only links inside the repository are checked. Absolute URLs (https:, mailto:) are never fetched:
a gate that depends on the network fails for reasons that are not the change under test.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

from .public_text import ROOT

# Inline links and images, `[text](target "title")`, and reference definitions, `[id]: target`.
INLINE = re.compile(r"!?\[(?:[^\]\\]|\\.)*\]\(\s*(<[^>]*>|[^)\s]*)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)", re.M)
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", re.M)
HTML_ID = re.compile(r"""<a\s[^>]*\b(?:id|name)=["']([^"']+)["']""", re.I)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1", re.S)
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def doc_files(root: Path = ROOT) -> list[Path]:
    # Every top-level Markdown file: README, CONTRIBUTING, SECURITY, CODE_OF_CONDUCT and whatever joins them.
    out = sorted(p for p in root.glob("*.md") if p.is_file())
    out += sorted((root / "docs").rglob("*.md")) if (root / "docs").is_dir() else []
    return out


def unfenced(text: str) -> str:
    """The text with fenced code blocks blanked: a heading or link written inside one is an example."""
    lines, out, fence = text.split("\n"), [], None
    for line in lines:
        m = FENCE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line.strip(m.group(1)[0]):
                fence = None
            out.append("")
        elif m:
            fence = m.group(1)
            out.append("")
        else:
            out.append(line)
    return "\n".join(out)


def prose(text: str) -> str:
    """Also without code spans, which can hold link-shaped examples."""
    return CODE_SPAN.sub(lambda m: " " * len(m.group(0)), unfenced(text))


def slug(heading: str) -> str:
    """The anchor GitHub gives a heading: markup dropped, lower case, punctuation removed, spaces
    turned into hyphens."""
    h = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    h = h.replace("`", "").replace("*", "")
    h = re.sub(r"<[^>]+>", "", h).strip().lower()
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")


def anchors(text: str) -> set[str]:
    found: set[str] = set()
    counts: dict[str, int] = {}
    body = unfenced(text)
    for m in HEADING.finditer(body):
        s = slug(m.group(2))
        n = counts.get(s, 0)
        counts[s] = n + 1
        found.add(s if n == 0 else f"{s}-{n}")
    found.update(HTML_ID.findall(text))
    return found


def targets(text: str) -> list[tuple[int, str]]:
    body = prose(text)
    out = []
    for rx in (INLINE, DEFINITION):
        for m in rx.finditer(body):
            t = m.group(1)
            if t.startswith("<") and t.endswith(">"):
                t = t[1:-1]
            out.append((body.count("\n", 0, m.start()) + 1, t))
    return sorted(out)


def check_text(text: str, source: Path, root: Path = ROOT, cache: dict | None = None) -> list[str]:
    cache = {} if cache is None else cache
    problems = []
    for line, target in targets(text):
        if not target or SCHEME.match(target) or target.startswith("//"):
            continue
        path_part, _, frag = target.partition("#")
        path_part = unquote(path_part.partition("?")[0])
        frag = unquote(frag)
        where = f"{source.relative_to(root).as_posix()}:{line}"
        if path_part.startswith("/"):
            dest = root / path_part.lstrip("/")
        else:
            dest = (source.parent / path_part) if path_part else source
        try:
            dest = dest.resolve()
            dest.relative_to(root.resolve())
        except ValueError:
            problems.append(f"{where}: link leaves the repository: {target}")
            continue
        if not dest.exists():
            problems.append(f"{where}: no such file: {target}")
            continue
        if frag and dest.is_file() and dest.suffix.lower() == ".md":
            if dest not in cache:
                cache[dest] = anchors(dest.read_text(encoding="utf-8", errors="replace"))
            if frag.lower() not in cache[dest] and frag not in cache[dest]:
                problems.append(f"{where}: no heading for #{frag} in {dest.relative_to(root.resolve()).as_posix()}")
    return problems


def check(root: Path = ROOT, files: list[Path] | None = None) -> list[str]:
    cache: dict = {}
    problems = []
    for p in files if files is not None else doc_files(root):
        problems += check_text(p.read_text(encoding="utf-8"), p, root, cache)
    return problems
