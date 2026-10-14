"""Public-text check: nothing in this repository may carry internal process text.

The patterns are assembled from fragments so that this file does not match itself.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .cases import ROOT

_WORDS = [
    "al" + "era",
    r"\bagents?\b",
    "archi" + "tect",
    "revi" + r"ew(er)?",
    "rul" + "ing",
    r"\bunit[- ]?[0-9]",
    "PROC" + "EDURE",
    "REQUIRE" + "MENTS",
    "HAND" + "OFF",
    "PLAN" + r"\.md",
    "RULES" + r"\.md",
    "DESIGN" + r"\.md",
    "\u00a7" + "[0-9]",
    "STAN" + "DARDS",
    "LEARN" + "INGS",
    "sw" + "arm",
    "wai" + "v",
    "oneyaml-" + "inter" + "nal",
    "projects/recursive-ai-" + "oss/oneyaml",
    "/x/" + "al" + "era",
    "/ho" + "me/",
]
# Identifiers are matched in the case they are written in. A data key such as `k1` or a
# variable `p2` is ordinary text; only the upper case form is an internal identifier.
_IDS = [
    r"\bphase [A-Z0-9]",
    r"\b(R[0-9]a?|P[0-9]|Q[0-9](\.[0-9])?|NN-[0-9]|K[0-9])\b",
    r"\b(T[0-3]|L[0-7])\b",
]
PATTERN = re.compile("(?i:" + "|".join(_WORDS) + ")|" + "|".join(_IDS))
HASH = re.compile(r"\b[0-9a-f]{40}\b")

# Vendored third-party data is not ours to edit and is checked by its digest instead.
SKIP_PREFIXES = ("suites/yaml-test-suite/data-",)
# Files that may mention a commit hash (provenance records and lockfiles).
HASH_OK_NAMES = {"SOURCE.md", "Cargo.lock", "pins.json"}


def repo_files(root: Path = ROOT) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root, capture_output=True, check=True).stdout
        names = [n for n in out.decode("utf-8").split("\0") if n]
        return [root / n for n in names if (root / n).is_file()]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts
                and "out" not in p.relative_to(root).parts[:1]]


def scan(paths: list[Path], root: Path = ROOT) -> list[str]:
    hits = []
    for p in paths:
        try:
            rel = p.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = p.as_posix()
        if rel.startswith(SKIP_PREFIXES) or rel == "suites/yaml-test-suite/LICENSE":
            continue
        vendored = p.name in ("input.yaml", "expected.json") or "/LICENSES/" in rel
        if rel.startswith("bench/workloads/real/") and vendored:
            continue  # third-party files, and values read from them, are checked by their digests
        data = p.read_bytes()
        if b"\0" in data[:4096] and not data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
            continue  # binary input data is checked by its digest
        text = data.decode("utf-8", errors="replace")
        for n, line in enumerate(text.split("\n"), 1):
            m = PATTERN.search(line)
            if m:
                hits.append(f"{rel}:{n}: {m.group(0)!r}: {line.strip()[:100]}")
            h = HASH.search(line)
            if h and p.name not in HASH_OK_NAMES and "revision" not in line.lower() \
                    and "uses:" not in line:  # a pinned action is a provenance line too
                hits.append(f"{rel}:{n}: commit hash outside a provenance line")
    return hits
