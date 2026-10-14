"""The host: what it is, how busy it is, and a sampler that watches it during a run."""

from __future__ import annotations

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
    return {
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


def git_revision(root: Path) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=root,
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=root,
                               capture_output=True, text=True, check=True).stdout.strip()
        return out + ("+modified" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return None


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
