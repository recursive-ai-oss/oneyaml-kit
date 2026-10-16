"""The host a run happened on: hardware, system, load and the kit revision."""

from __future__ import annotations

import os
import platform
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def loadavg() -> tuple[float, float, float]:
    parts = read("/proc/loadavg").split()
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except (IndexError, ValueError):
        return (0.0, 0.0, 0.0)


def cpu_model() -> str:
    for line in read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def memory_total_gb() -> float:
    for line in read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            return round(int(line.split()[1]) / 1024 / 1024, 1)
    return 0.0


def git_revision(root: Path) -> str | None:
    """The short revision of the checkout at `root`, or the one the launcher names in KIT_REVISION
    (a tree unpacked without history has none of its own)."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True,
                             text=True, check=True).stdout.strip()
        if Path(top).resolve() == root.resolve():
            return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=root, capture_output=True,
                                  text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    return (os.environ.get("KIT_REVISION") or "").strip() or None


def renderer_revision(root: Path) -> str | None:
    """The revision of the kit code that is running, for text it generates: the short commit of the checkout,
    with "+modified" when tracked files differ from it. Without a checkout, the revision the launcher names."""
    rev = git_revision(root)
    if rev is None:
        return None
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True,
                             text=True, check=True).stdout.strip()
        if Path(top).resolve() == root.resolve() and subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, capture_output=True,
                text=True, check=True).stdout.strip():
            return rev + "+modified"
    except (OSError, subprocess.CalledProcessError):
        pass
    return rev


def release_tag(src: Path, version: str) -> str | None:
    """The release tag of the commit `version` (a revision, full or abbreviated) in the library checkout
    at `src`, when that commit carries exactly one version tag. The checkout need not be at that commit.
    Anything else gives None: a tag is never guessed."""
    def git(*args: str) -> str | None:
        try:
            return subprocess.run(["git", "-C", str(src), *args], capture_output=True, text=True,
                                  check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None
    if not re.fullmatch(r"[0-9a-f]{7,40}", version or ""):
        return None
    commit = git("rev-parse", "--verify", "--quiet", version + "^{commit}")
    if not commit or not commit.startswith(version):
        return None
    tags = [t for t in (git("tag", "--points-at", commit) or "").split() if re.fullmatch(r"v\d\S*", t)]
    return tags[0] if len(tags) == 1 else None


def describe(revision: str | None) -> dict:
    """The environment record stored with every result."""
    env = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cpu": cpu_model(),
        "cpus": os.cpu_count() or 1,
        "memory_gb": memory_total_gb(),
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "kit_revision": revision,
    }
    # A rented machine says what it is, since the CPU model alone does not name the instance type.
    if os.environ.get("KIT_INSTANCE_TYPE"):
        env["instance_type"] = os.environ["KIT_INSTANCE_TYPE"]
    return env
