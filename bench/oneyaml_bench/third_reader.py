"""A third, independent reading of the real-file workloads.

The frozen `expected.json` of each real file was accepted because two YAML 1.2 parsers (oneyaml
and saphyr) agree on it, and the Go harness checks dumps with yaml.v3 and goccy. This module
adds the `yaml` package for JavaScript (eemeli/yaml). It is a different code base in a different
language, written for the YAML 1.2 core schema from the start, with its own lexer and composer;
it is not one of the libraries the harness measures, and it shares no code with the two
freezing parsers or with the Go referees.

The reader runs from a staging directory under the kit's output directory, where its one
pinned dependency is installed with `npm ci` from the lock file kept in `bench/referee-js`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import freeze

SRC = Path(__file__).resolve().parents[1] / "referee-js"
KIT_ROOT = Path(__file__).resolve().parents[2]
FILES = ("package.json", "package-lock.json", "read.mjs")


class ReaderUnavailable(RuntimeError):
    """Node or the pinned package could not be set up. Never turned into a pass."""


def stage_dir() -> Path:
    return Path(os.environ.get("KIT_OUT", KIT_ROOT / "out")) / "referee-js"


def stage() -> Path:
    """Installs the pinned reader once per version of its files and returns the script path."""
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        raise ReaderUnavailable("node and npm are needed for the third reader")
    dest = stage_dir()
    digest = hashlib.sha256(b"".join((SRC / f).read_bytes() for f in FILES)).hexdigest()
    stamp = dest / ".installed"
    if not (stamp.is_file() and stamp.read_text() == digest and (dest / "node_modules" / "yaml").is_dir()):
        dest.mkdir(parents=True, exist_ok=True)
        for f in FILES:
            shutil.copyfile(SRC / f, dest / f)
        r = subprocess.run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=dest,
                           capture_output=True, text=True)
        if r.returncode:
            raise ReaderUnavailable("npm ci failed: " + r.stderr[-400:])
        stamp.write_text(digest)
    return dest / "read.mjs"


def read(files: list[Path]) -> dict[str, dict]:
    """The value each file reads to: {"value": ...} or {"error": ...}, keyed by path."""
    script = stage()
    r = subprocess.run([shutil.which("node"), str(script), *map(str, files)], capture_output=True, text=True)
    if r.returncode:
        raise ReaderUnavailable("the reader failed: " + r.stderr[-400:])
    return json.loads(r.stdout)


def check(real_dir: Path) -> dict[str, list]:
    """Differences between each real workload's expected.json and the third reading.

    The result maps a workload to its differences as (path, expected, third reader) tuples;
    an empty list means the reader confirms the expected value.
    """
    dirs = sorted(p for p in real_dir.iterdir() if p.is_dir() and (p / "input.yaml").is_file())
    got = read([d / "input.yaml" for d in dirs])
    out = {}
    for d in dirs:
        r = got[str(d / "input.yaml")]
        if "error" in r:
            out[d.name] = [("/", "a value", "refused: " + r["error"])]
            continue
        expected = json.loads((d / "expected.json").read_text(encoding="utf-8"))
        out[d.name] = freeze.diff(expected, r["value"])
    return out
