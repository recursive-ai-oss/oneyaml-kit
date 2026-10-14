"""Freezing the expected value of a real-world workload.

The expected value of a vendored file is not any one library's output. It is accepted
only when two independent YAML 1.2 parsers read the file to the same value, and the
freeze also reports every place a YAML 1.1 reader (PyYAML, when installed) reads it
differently, so each scalar whose meaning depends on the schema version has been looked at.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def diff(a, b, path=""):
    """Differences between two JSON-like values as (path, a, b)."""
    if type(a) is not type(b):
        return [(path or "/", a, b)]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append((f"{path}/{k}", a.get(k, "<absent>"), b.get(k, "<absent>")))
            else:
                out += diff(a[k], b[k], f"{path}/{k}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [(path or "/", f"{len(a)} items", f"{len(b)} items")]
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in diff(x, y, f"{path}/{i}")]
    return [] if a == b else [(path or "/", a, b)]


def candidates(harness_cmd: list[str], file: Path) -> dict:
    out = subprocess.run(harness_cmd + ["candidates", str(file)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def schema_1_1_differences(file: Path, value) -> list | None:
    """Where PyYAML (YAML 1.1) reads the file differently; None when it is not installed."""
    try:
        import yaml  # type: ignore
    except ImportError:
        return None

    def norm(x):
        if isinstance(x, dict):
            return {(str(k).lower() if isinstance(k, bool) else str(k)): norm(v) for k, v in x.items()}
        if isinstance(x, list):
            return [norm(v) for v in x]
        return x

    docs = [norm(d) for d in yaml.safe_load_all(file.read_text(encoding="utf-8"))]
    other = docs if len(docs) != 1 else docs[0]
    return diff(value, other)


def freeze(harness_cmd: list[str], real_dir: Path, write: bool = True, log=print) -> int:
    """Returns the number of workloads whose two parsers disagree."""
    bad = 0
    for d in sorted(p for p in real_dir.iterdir() if p.is_dir()):
        f = d / "input.yaml"
        c = candidates(harness_cmd, f)
        a, b = c["oneyaml"], c["saphyr"]
        if "error" in a or "error" in b:
            log(f"{d.name}: a parser refused the file: {a.get('error') or b.get('error')}")
            bad += 1
            continue
        dd = diff(a["value"], b["value"])
        if dd:
            log(f"{d.name}: the two parsers disagree at {dd[:3]}")
            bad += 1
            continue
        for path, x, y in (schema_1_1_differences(f, a["value"]) or [])[:5]:
            log(f"{d.name}: YAML 1.1 reads {path} as {y!r}, YAML 1.2 as {x!r}")
        text = json.dumps(a["value"], ensure_ascii=False, separators=(",", ":")) + "\n"
        target = d / "expected.json"
        if write:
            target.write_text(text, encoding="utf-8")
        elif target.read_text(encoding="utf-8") != text:
            log(f"{d.name}: expected.json is out of date")
            bad += 1
            continue
        log(f"{d.name}: frozen")
    return bad
