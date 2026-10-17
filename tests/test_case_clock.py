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
