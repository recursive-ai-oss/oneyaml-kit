"""The host: what it is, how busy it is, and a sampler that watches it during a run."""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import threading
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


def describe(git_revision: str | None = None, pinned_cpu: int | None = None) -> dict:
    """The environment record stored with every result."""
    boost = read("/sys/devices/system/cpu/cpufreq/boost")
    env = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cpu": cpu_model(),
        "cpus": os.cpu_count() or 1,
        "memory_gb": memory_total_gb(),
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "python": platform.python_version(),
        "governor": read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") or "unknown",
        "boost": {"1": "enabled", "0": "disabled"}.get(boost, "unknown"),
        "pinned_cpu": pinned_cpu,
        "kit_revision": git_revision,
    }
    # Which kind of machine, when the launcher says (a cloud instance type). Shards of one run must match.
    if os.environ.get("KIT_INSTANCE_TYPE"):
        env["instance_type"] = os.environ["KIT_INSTANCE_TYPE"]
    return env


def _git(root: Path, *args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def git_revision(root: Path, env=None) -> str | None:
    """The revision of the kit at `root`, with `+modified` when a file that decides a result differs.

    A revision is accepted only from a git checkout whose top level is the kit root itself: a kit
    unpacked inside some other repository would otherwise report that repository's commit. A kit
    without a checkout of its own (an unpacked archive) takes the revision the launcher names in
    KIT_REVISION, which it fixed before the run began; with neither the revision is unknown.
    """
    env = os.environ if env is None else env
    top = _git(root, "rev-parse", "--show-toplevel")
    if top is not None and Path(top).resolve() == root.resolve():
        rev = _git(root, "rev-parse", "--short=12", "HEAD")
        dirty = _git(root, "status", "--porcelain", "--", *DIGEST_DIRS)
        if rev is None or dirty is None:
            return None
        return rev + ("+modified" if dirty else "")
    given = (env.get("KIT_REVISION") or "").strip()
    return given or None


# The parts of the kit whose files decide what a run measures and how it is judged: the driver and
# harnesses with the workloads and known-deviation lists (bench), the case data (suites), the
# adapters, the tools and the launchers (bin). Documentation, the gate's own tests and CI files do
# not change a result and are left out.
DIGEST_DIRS = ("bench", "suites", "adapters", "tools", "bin")
# Build output and editor leftovers that may sit in a tree without being part of the kit: they are
# skipped by name so a machine that built something still matches one that did not. The top-level `bin`
# (the launchers) is walked from its root, so only a nested `bin` is skipped.
DIGEST_SKIP_DIRS = {"__pycache__", "target", "out", "artifacts", "obj", "bin", "node_modules", ".bin", ".git"}
DIGEST_SKIP_SUFFIXES = (".pyc", ".swp", "~")
_digests: dict[str, str] = {}


def kit_digest(root: Path) -> str:
    """A hash of the kit's files (see DIGEST_DIRS), computed from their bytes and relative paths.

    It needs no git checkout, so a machine that unpacked an archive gets the same answer as the
    checkout the archive came from. Two shards with different digests ran different code or data.
    """
    key = str(root)
    if key in _digests:
        return _digests[key]
    top = hashlib.sha256()
    for d in DIGEST_DIRS:
        base = root / d
        entries = []
        for dirpath, dirnames, filenames in os.walk(base):
            rel = Path(dirpath).relative_to(root)
            dirnames[:] = sorted(n for n in dirnames if n not in DIGEST_SKIP_DIRS)
            for name in sorted(filenames):
                if name.endswith(DIGEST_SKIP_SUFFIXES) or name == ".DS_Store":
                    continue
                entries.append(rel / name)
        for rel_path in sorted(entries):
            top.update(rel_path.as_posix().encode("utf-8") + b"\0")
            top.update(hashlib.sha256((root / rel_path).read_bytes()).digest())
    _digests[key] = "sha256:" + top.hexdigest()
    return _digests[key]


def _cpu_times() -> tuple[int, int]:
    """(busy, total) jiffies over all CPUs."""
    for line in read("/proc/stat").splitlines():
        if line.startswith("cpu "):
            f = [int(x) for x in line.split()[1:]]
            idle = f[3] + (f[4] if len(f) > 4 else 0)
            return sum(f[:8]) - idle, sum(f[:8])
    return 0, 0


def _process_jiffies(pid: int) -> int:
    """utime + stime of a process and its reaped children, in jiffies."""
    raw = read(f"/proc/{pid}/stat")
    if ")" not in raw:
        return 0
    f = raw.rsplit(")", 1)[1].split()
    try:
        return int(f[11]) + int(f[12]) + int(f[13]) + int(f[14])
    except (IndexError, ValueError):
        return 0


class Sampler:
    """Samples how many cores are busy with work other than the watched process.

    Every `interval` seconds it reads the all-CPU counters and the watched process's own
    CPU time. The difference, converted to cores, is the foreign load a timing was exposed
    to. The one-minute load average cannot show this: it lags by a minute and counts
    runnable threads rather than busy cores.
    """

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self.pid: int | None = None
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._hz = os.sysconf("SC_CLK_TCK")

    def watch(self, pid: int | None) -> None:
        self.pid = pid

    def start(self) -> None:
        self._thread.start()

    def _loop(self) -> None:
        prev_busy, prev_total = _cpu_times()
        prev_own = _process_jiffies(self.pid) if self.pid else 0
        while not self._stop.wait(self.interval):
            busy, total = _cpu_times()
            own = _process_jiffies(self.pid) if self.pid else 0
            dt = total - prev_total
            if dt > 0:
                cores = os.cpu_count() or 1
                busy_cores = (busy - prev_busy) / dt * cores
                own_cores = max(0, own - prev_own) / dt * cores if self.pid else 0.0
                self.samples.append(max(0.0, busy_cores - own_cores))
            prev_busy, prev_total, prev_own = busy, total, own

    def stop(self) -> list[float]:
        self._stop.set()
        self._thread.join(timeout=2)
        return self.samples
