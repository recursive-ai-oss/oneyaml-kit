"""Subprocess adapter connection: JSON Lines over stdin/stdout.

One adapter process serves every case of a run, so start-up cost is paid once.
A crash, a protocol violation or a timeout is reported for that case only; the
process is then killed and restarted for the next one.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from . import kitjson
from .cases import b64

PROTOCOL = 2
# Hello versions the runner accepts. 1 is the original protocol; 2 adds the `accepted` answer
# and the capabilities `error-events` and `outcome:<op>` (docs/adapter-protocol.md).
SUPPORTED_PROTOCOLS = (1, 2)
_EOF = object()
POLL_SECONDS = 0.05


def _status_kb(pid: int, field: str) -> int | None:
    """A memory field of /proc/<pid>/status in kB (Linux only), else None."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith(field + ":"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _reset_peak(pid: int) -> bool:
    """Reset the kernel's peak-RSS mark of a process, so the next read covers one case."""
    try:
        with open(f"/proc/{pid}/clear_refs", "w") as f:
            f.write("5")
        return True
    except OSError:
        return False


def tree_pids(session: int) -> list[int] | None:
    """Every live process of the adapter's session: the process the kit started and all that
    it launched, whatever the launcher (a shell, `go run`, `dotnet run`). The adapter is
    started in its own session so that this is well defined and orphans stay included.
    None where the platform has no /proc."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    out = []
    for n in names:
        if not n.isdigit():
            continue
        try:
            with open(f"/proc/{n}/stat") as f:
                stat = f.read()
            # Fields after the command name, which may itself contain spaces or brackets.
            rest = stat[stat.rindex(")") + 2:].split()
            if int(rest[3]) == session:
                out.append(int(n))
        except (OSError, ValueError, IndexError):
            continue
    return out


def tree_kb(pids: list[int], field: str) -> int:
    return sum(_status_kb(p, field) or 0 for p in pids)


class AdapterError(Exception):
    """The adapter could not be started or did not complete the handshake."""


class SubprocessAdapter:
    def __init__(self, command: list[str], cwd: Path | None = None, env: dict | None = None,
                 timeout: float = 10.0, memory_limit_mb: int | None = None):
        self.command = command
        self.cwd = cwd
        self.env = env
        self.timeout = timeout
        self.memory_limit_mb = memory_limit_mb
        self.proc: subprocess.Popen | None = None
        self.hello: dict = {}
        self._q: queue.Queue = queue.Queue()
        self.restarts = 0

    # -- lifecycle
    def start(self) -> dict:
        preexec = None
        if self.memory_limit_mb and os.name == "posix":
            import resource

            limit = self.memory_limit_mb * 1024 * 1024

            def preexec():  # runs in the child before exec
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

        try:
            self.proc = subprocess.Popen(
                self.command, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0, preexec_fn=preexec,
                start_new_session=(os.name == "posix"))
        except OSError as e:
            raise AdapterError(f"cannot start {self.command[0]}: {e}") from e
        self._q = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc, self._q), daemon=True).start()
        self._send({"type": "hello", "protocol": PROTOCOL})
        h = self._recv(self.timeout * 3)
        if not isinstance(h, dict) or h.get("type") != "hello" or h.get("protocol") not in SUPPORTED_PROTOCOLS:
            self.stop()
            raise AdapterError(f"bad hello from adapter: {h!r}")
        for k in ("name", "version", "language", "capabilities"):
            if k not in h:
                self.stop()
                raise AdapterError(f"hello is missing {k!r}")
        self.hello = h
        return h

    @staticmethod
    def _pump(proc, q) -> None:
        buf = b""
        out = proc.stdout
        while True:
            chunk = os.read(out.fileno(), 1 << 20)
            if not chunk:
                q.put(_EOF)
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                q.put(line)

    def _send(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n")

    def _recv(self, timeout: float, watch=None):
        """Next response line. `watch`, if given, is called about every 50 ms while waiting and
        returns a reason string to abort."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return "timeout"
            try:
                line = self._q.get(timeout=min(left, POLL_SECONDS) if watch else left)
            except queue.Empty:
                if watch:
                    why = watch()
                    if why:
                        return ("abort", why)
                continue
            break
        if line is _EOF:
            return "eof"
        try:
            return kitjson.loads(line)
        except (ValueError, RecursionError):
            return "garbage"

    def _kill_tree(self, session: int) -> None:
        """Kill everything of the adapter's session: the process and whatever it launched."""
        for pid in tree_pids(session) or []:
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    def stop(self) -> None:
        p = self.proc
        self.proc = None
        if not p:
            return
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=1)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        # A launcher may leave children behind; none of them may outlive the adapter.
        self._kill_tree(p.pid)
        try:
            p.stdout.close()
        except OSError:
            pass

    def close(self) -> None:
        if self.proc:
            try:
                self._send({"type": "bye"})
            except OSError:
                pass
        self.stop()

    # -- requests
    def run(self, case_id: str, op: str, data: bytes, args: dict, limits: dict | None = None) -> dict:
        """Send one case. `limits` ({"time_seconds", "memory_mb"}) replaces the manifest timeout
        for this case, is enforced while the case runs, and makes the response carry `_measure`:
        wall-clock seconds and peak resident-memory growth in MB (None where unmeasurable)."""
        if self.proc is None:
            self.start()
            self.restarts += 1
        pid = self.proc.pid
        timeout, watch, base_kb, measurable = self.timeout, None, 0, False
        seen = [0]
        if limits:
            timeout = limits["time_seconds"]
            # The whole process tree counts, not just the process the kit started: a launcher
            # that does not exec the library would otherwise hide all of its memory.
            members = tree_pids(pid)
            measurable = bool(members) and all(_reset_peak(p) for p in members)
            base_kb = tree_kb(members or [], "VmRSS")
            cap_kb = limits["memory_mb"] * 1024

            def watch():
                cur = tree_kb(tree_pids(pid) or [], "VmRSS")
                seen[0] = max(seen[0], cur)
                if cur - base_kb > cap_kb:
                    return f"memory growth passed the {limits['memory_mb']:g} MB bound"
                return None

        req = {"type": "run", "id": case_id, "op": op, "input": b64(data), "args": args}
        t0 = time.monotonic()
        try:
            self._send(req)
        except OSError:
            return self._failed("crash", "adapter closed its input")
        r = self._recv(timeout, watch)
        secs = time.monotonic() - t0
        peak = None
        if limits and measurable:
            after = tree_pids(pid) or []
            # Per-process peaks of everything alive now, and the highest sum seen while waiting.
            high = max(seen[0], tree_kb(after, "VmHWM"))
            peak = max(0, high - base_kb) / 1024
        if isinstance(r, tuple):
            out = self._failed("memory", r[1])
        elif r == "timeout":
            out = self._failed("timeout", f"no answer within {timeout:g} s")
        elif r == "eof":
            code = self.proc.poll() if self.proc else None
            out = self._failed("crash", f"adapter exited (code {code})")
        elif r == "garbage" or not isinstance(r, dict) or r.get("id") != case_id:
            out = self._failed("crash", "adapter sent a malformed or mismatched response")
        else:
            out = r
        if limits:
            out["_measure"] = {"seconds": secs, "peak_mb": peak, "time_bound": timeout}
        return out

    def _failed(self, status: str, detail: str) -> dict:
        if self.proc:
            self._kill_tree(self.proc.pid)
        self.stop()
        return {"status": status, "detail": detail}
