"""Subprocess adapter connection: JSON Lines over stdin/stdout.

One adapter process serves every ordinary case of a run, so start-up cost is paid once.
A case with resource bounds gets a process of its own (`run_fresh`), so what it uses is
measured from a clean start and never depends on the cases that ran before it.
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
EXIT_WAIT_SECONDS = 5.0
POLL_SECONDS = 0.05
# The trivial input a fresh bounded-case session answers before the case itself, whatever the
# answer is: a plain scalar, valid in every operation.
WARM_UP_INPUT = b"a\n"
# How many restarts of the shared session may fail one after another before the rest of the run
# stops trying (see SubprocessAdapter.run).
MAX_FAILED_STARTS = 3


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
                 timeout: float = 10.0, memory_limit_mb: int | None = None, clock=time.monotonic):
        self.command = command
        # Reads the time a bounded case is charged for. A parameter so that a test can say exactly
        # which moments the clock is read at, without depending on how fast the host is.
        self.clock = clock
        self.cwd = cwd
        self.env = env
        self.timeout = timeout
        self.memory_limit_mb = memory_limit_mb
        self.proc: subprocess.Popen | None = None
        self.hello: dict = {}
        # The hello the runner accepted at the first start. A later start of the same adapter
        # (a restart, a bounded case's own process) must say the same, because the runner decided
        # what to send, and how to grade, from the first one.
        self.expected_hello: dict | None = None
        self._q: queue.Queue = queue.Queue()
        self.restarts = 0
        self.fresh_sessions = 0
        self.failed_starts = 0

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
        except (OSError, subprocess.SubprocessError) as e:
            # SubprocessError is what a failing pre-exec step (the address-space limit) raises.
            raise AdapterError(f"cannot start {self.command[0]}: {e}") from e
        self._q = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc, self._q, self.clock), daemon=True).start()
        try:
            self._send({"type": "hello", "protocol": PROTOCOL})
        except OSError as e:
            # The process may be gone before it reads anything; that is a failed start, not a
            # reason to stop the run.
            self.stop()
            raise AdapterError(f"adapter closed its input before the hello: {e}") from e
        h, _ = self._recv(self.timeout * 3)
        if not isinstance(h, dict) or h.get("type") != "hello" or h.get("protocol") not in SUPPORTED_PROTOCOLS:
            self.stop()
            raise AdapterError(f"bad hello from adapter: {h!r}")
        for k in ("name", "version", "language", "capabilities"):
            if k not in h:
                self.stop()
                raise AdapterError(f"hello is missing {k!r}")
        if self.expected_hello is not None:
            changed = self._hello_changes(self.expected_hello, h)
            if changed:
                self.stop()
                raise AdapterError("hello differs from the first start: " + "; ".join(changed))
        self.hello = h
        return h

    @staticmethod
    def _hello_changes(first: dict, now: dict) -> list[str]:
        """What a later hello says differently from the first one (lists compare as sets)."""
        out = []
        for k in ("protocol", "name", "version", "language"):
            if first.get(k) != now.get(k):
                out.append(f"{k} {first.get(k)!r} became {now.get(k)!r}")
        for k in ("capabilities", "emulates"):
            a, b = first.get(k, []), now.get(k, [])
            try:
                a, b = set(a), set(b)
            except TypeError:
                out.append(f"{k} is not a list of strings")
                continue
            if a != b:
                out.append(f"{k} changed (lost {sorted(a - b)}, gained {sorted(b - a)})")
        return out

    @staticmethod
    def _pump(proc, q, clock=time.monotonic) -> None:
        """Cut the adapter's output into lines. Each line is queued with the time its last byte was
        read, so the time a case is charged for ends when the adapter has finished answering and
        does not include what the kit does with the answer afterwards.

        Pieces are kept in a list and joined once: appending every read to one growing buffer
        copies the whole line again for each read, which for an answer of tens of megabytes (a pipe
        hands over at most 64 KiB at a time) is quadratic and slower than the adapter itself."""
        parts: list[bytes] = []
        out = proc.stdout
        while True:
            chunk = os.read(out.fileno(), 1 << 20)
            now = clock()
            if not chunk:
                q.put((_EOF, now))
                return
            # Only the new bytes are searched for a line break.
            start = 0
            while True:
                i = chunk.find(b"\n", start)
                if i < 0:
                    break
                parts.append(chunk[start:i])
                q.put((parts[0] if len(parts) == 1 else b"".join(parts), now))
                parts = []
                start = i + 1
            if start < len(chunk):
                parts.append(chunk[start:])

    def _send(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n")

    def _recv(self, timeout: float, watch=None):
        """Next response line. `watch`, if given, is called about every 50 ms while waiting and
        returns a reason string to abort. Returns (answer, time the answer's last byte arrived);
        the time is None when nothing arrived."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return "timeout", None
            try:
                line = self._q.get(timeout=min(left, POLL_SECONDS) if watch else left)
            except queue.Empty:
                if watch:
                    why = watch()
                    if why:
                        return ("abort", why), None
                continue
            break
        line, arrived = line
        if line is _EOF:
            return "eof", arrived
        try:
            return kitjson.loads(line), arrived
        except (ValueError, RecursionError):
            return "garbage", arrived

    def _exit_detail(self) -> str:
        """Say how the adapter ended. Its output closes a moment before the process is reaped,
        so the exit code is waited for rather than polled once."""
        if self.proc is None:
            return "adapter exited"
        try:
            code = self.proc.wait(timeout=EXIT_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            return "adapter closed its output but did not exit"
        if code < 0:
            return f"adapter exited (killed by signal {-code})"
        return f"adapter exited (code {code})"

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
            # The session died on an earlier case. A restart that fails fails this case and
            # nothing else; the next case tries again, up to MAX_FAILED_STARTS times in a row,
            # after which the rest of the run is refused at once rather than paying a start-up
            # timeout for every remaining case.
            if self.failed_starts >= MAX_FAILED_STARTS:
                return self._not_run(f"adapter is not running: {MAX_FAILED_STARTS} restarts in a row failed", limits)
            try:
                self.start()
            except (AdapterError, OSError, subprocess.SubprocessError) as e:
                self.failed_starts += 1
                return self._not_run(f"adapter could not be restarted: {e}", limits)
            self.failed_starts = 0
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
        try:
            self._send(req)
        except OSError:
            return self._failed("crash", "adapter closed its input")
        # The clock runs from the moment the request is completely written to the moment the
        # answer's last byte was read: the adapter's work after it has the request (all but the
        # last pipe buffer of it), plus the answer's trip through the pipe. Building the request,
        # decoding the answer and watching memory are the kit's own work and are not charged.
        t0 = self.clock()
        r, arrived = self._recv(timeout, watch)
        secs = (arrived if arrived is not None else self.clock()) - t0
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
            out = self._failed("crash", self._exit_detail())
        elif r == "garbage" or not isinstance(r, dict) or r.get("id") != case_id:
            out = self._failed("crash", "adapter sent a malformed or mismatched response")
        else:
            out = r
        if limits:
            out["_measure"] = {"seconds": secs, "peak_mb": peak, "time_bound": timeout}
        return out

    def run_fresh(self, case_id: str, op: str, data: bytes, args: dict, limits: dict) -> dict:
        """Run one bounded case in a new adapter process that serves nothing else.

        Memory is measured as growth over what the process holds when the case starts. A
        process that has already served cases holds memory its runtime kept (a garbage
        collector's heap, a parser's buffers), so the same case measures differently after
        different predecessors. A new process starts from the same state every time. It first
        answers one trivial request of the same operation, so what the library and its runtime
        do on first use (compiling code, loading types) is paid before the case is measured and
        is not counted against its bounds. The shared session is left alone and serves the next
        ordinary case. A process that cannot be started, or dies on the trivial request, fails
        this case and nothing else."""
        one = SubprocessAdapter(self.command, cwd=self.cwd, env=self.env, timeout=self.timeout,
                                memory_limit_mb=self.memory_limit_mb)
        one.expected_hello = self.expected_hello
        self.fresh_sessions += 1

        def failed(detail: str) -> dict:
            return self._not_run(detail, limits)

        try:
            try:
                one.start()
            except (AdapterError, OSError, subprocess.SubprocessError) as e:
                return failed(f"fresh session did not start: {e}")
            warm = one.run("warm-up", op, WARM_UP_INPUT, {"patch": []} if op == "patch" else {})
            if warm.get("status") in ("crash", "timeout", "memory"):
                return failed(f"fresh session failed its warm-up request: {warm.get('detail', '')}")
            return one.run(case_id, op, data, args, limits)
        finally:
            one.close()

    @staticmethod
    def _not_run(detail: str, limits: dict | None) -> dict:
        """The answer for a case that never reached a running adapter."""
        out = {"status": "crash", "detail": detail}
        if limits:
            out["_measure"] = {"seconds": 0.0, "peak_mb": None, "time_bound": limits["time_seconds"]}
        return out

    def _failed(self, status: str, detail: str) -> dict:
        if self.proc:
            self._kill_tree(self.proc.pid)
        self.stop()
        return {"status": status, "detail": detail}
