"""The time a bounded case is charged for is the adapter's work and nothing the kit does around it.

The tests drive the clock by hand, so no assertion depends on how fast the host is: the stub adapter
answers only after the test has moved the clock, and the moves that stand for the kit's own work
(building the request, decoding the answer, watching memory) happen at moments that must not count.
"""

import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401
from oneyaml_kit import kitjson
from oneyaml_kit import protocol as P
from oneyaml_kit.protocol import SubprocessAdapter

# Answers the hello, then holds each answer back until a line saying `go` arrives.
STUB = r'''
import json, sys
for line in sys.stdin:
    req = json.loads(line)
    if req["type"] == "hello":
        print(json.dumps({"type": "hello", "protocol": 2, "name": "stub", "version": "0",
                          "language": "python", "capabilities": ["events"]}), flush=True)
    elif req["type"] == "run":
        if sys.stdin.readline().strip() != "go":
            break
        print(json.dumps({"id": req["id"], "status": "ok", "events": []}), flush=True)
    else:
        break
'''


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class CaseClock(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, self.t)
        (self.t / "stub.py").write_text(STUB)
        self.clock = Clock()
        self.a = SubprocessAdapter([sys.executable, str(self.t / "stub.py")], timeout=30, clock=self.clock)
        self.addCleanup(self.a.close)
        self.a.start()

    def measure(self):
        clock, a = self.clock, self.a
        real_send = a._send

        def send(obj):
            if obj.get("type") != "run":
                return real_send(obj)
            clock.now += 200   # building a very large request
            real_send(obj)
            clock.now += 400   # the rest of the kit's write path, before the request counts as sent

        # The memory watch is the one hook that runs while the kit waits. Its second memory read
        # (the first is the starting level) is the adapter's turn: the stub is released only after
        # 30 s have passed on the test clock.
        calls = [0]

        def watched_kb(pids, field):
            calls[0] += 1
            if calls[0] == 2:
                clock.now += 30   # the adapter's own work
                a.proc.stdin.write(b"go\n")
            return 0

        real_loads = kitjson.loads

        def loads(line):
            clock.now += 500   # decoding the answer
            return real_loads(line)

        with mock.patch.object(a, "_send", send), mock.patch.object(P, "tree_kb", watched_kb), \
                mock.patch.object(kitjson, "loads", loads):
            r = a.run("c", "events", b"x\n", {}, {"time_seconds": 20000, "memory_mb": 100})
        return r

    def test_only_the_adapters_work_is_charged(self):
        r = self.measure()
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["_measure"]["seconds"], 30)


class WholeBound(CaseClock):
    """The write and the wait for the answer share one bound; the test clock decides how long each took."""

    def run_case(self, write_s, answer_s, bound=20):
        clock, a = self.clock, self.a
        real_send = a._send

        def send(obj):
            if obj.get("type") == "run":
                clock.now += write_s   # a reader that is slow to take the request
            return real_send(obj)

        calls = [0]

        def watched_kb(pids, field):
            calls[0] += 1
            if calls[0] == 2:   # the first read is the starting level; this one is the adapter's turn
                clock.now += answer_s
                a.proc.stdin.write(b"go\n")
            return 0

        with mock.patch.object(a, "_send", send), mock.patch.object(P, "tree_kb", watched_kb):
            return a.run("c", "events", b"x\n", {}, {"time_seconds": bound, "memory_mb": 100})

    def test_each_alone_within_the_bound_passes(self):
        r = self.run_case(15, 0)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["_measure"]["seconds"], 0)
        self.setUp()
        r = self.run_case(0, 15)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["_measure"]["seconds"], 15)

    def test_together_over_the_bound_times_out(self):
        r = self.run_case(15, 10)
        self.assertEqual(r["status"], "timeout")

    def test_the_answer_is_still_the_only_thing_charged(self):
        r = self.run_case(12, 7)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["_measure"]["seconds"], 7)


# Answers the hello and then never reads again, so a large request blocks in the pipe.
DEAF = r'''
import json, sys, time
json.loads(sys.stdin.readline())
print(json.dumps({"type": "hello", "protocol": 2, "name": "stub", "version": "0",
                  "language": "python", "capabilities": ["events"]}), flush=True)
time.sleep(60)
'''


class MemoryWhileWriting(unittest.TestCase):
    def test_the_peak_is_recorded_when_memory_aborts_the_write(self):
        t = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, t)
        (t / "deaf.py").write_text(DEAF)
        a = SubprocessAdapter([sys.executable, str(t / "deaf.py")], timeout=60)
        self.addCleanup(a.close)
        a.start()
        calls = [0]

        def kb(pids, field):
            calls[0] += 1
            return 0 if calls[0] == 1 else 512 * 1024   # starting level, then 512 MB

        with mock.patch.object(P, "tree_kb", kb):
            r = a.run("c", "events", b"x" * 8_000_000, {}, {"time_seconds": 50, "memory_mb": 100})
        self.assertEqual(r["status"], "memory")
        self.assertEqual(r["_measure"]["peak_mb"], 512.0)


class Pump(unittest.TestCase):
    def lines(self, pieces):
        r, w = os.pipe()
        proc = mock.Mock()
        proc.stdout.fileno.return_value = r
        import queue
        q = queue.Queue()

        def feed():
            for p in pieces:
                os.write(w, p)
            os.close(w)

        threading.Thread(target=feed, daemon=True).start()
        P.SubprocessAdapter._pump(proc, q, lambda: 7.0)
        os.close(r)
        out = []
        while not q.empty():
            out.append(q.get())
        return out

    def test_lines_are_cut_whatever_the_chunking(self):
        big = b"x" * 3_000_000
        out = self.lines([b"ab\ncd\n", b"e" * 10, b"f\n" + big + b"\n", b"tail"])
        self.assertEqual(out[:3], [(b"ab", 7.0), (b"cd", 7.0), (b"e" * 10 + b"f", 7.0)])
        self.assertEqual(out[3], (big, 7.0))
        self.assertIs(out[4][0], P._EOF)
        self.assertEqual(len(out), 5)  # the unterminated tail is not a line


if __name__ == "__main__":
    unittest.main()
