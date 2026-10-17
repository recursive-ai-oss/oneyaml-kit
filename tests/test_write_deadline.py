"""An adapter that stops reading cannot hang the kit: the request write has the case's deadline.

The tests inject a clock that moves a fixed step each time it is read, so the outcome does not
depend on how fast the host is and nothing asserts elapsed time."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401
from oneyaml_kit import protocol as P
from oneyaml_kit.protocol import SubprocessAdapter

HELLO = ('{"type":"hello","protocol":1,"name":"s","version":"0","language":"python",'
         '"capabilities":["events"],"emulates":[]}')

# MODE=none: never reads another byte after the hello. MODE=half: reads a little of the request,
# then stalls. MODE=ok: answers normally (the restarted adapter).
STUB = f'''
import os, sys, time
mode = os.environ.get("MODE", "ok")
sys.stdout.write({HELLO!r} + "\\n"); sys.stdout.flush()
sys.stdin.buffer.readline()
if mode == "none":
    time.sleep(20)
if mode == "half":
    os.read(0, 65536); time.sleep(20)
while True:
    line = sys.stdin.buffer.readline()
    if not line or b"bye" in line[:20]:
        break
    sys.stdout.write('{{"id":"c","status":"ok","events":["+STR","-STR"]}}\\n'); sys.stdout.flush()
'''


class Stepping:
    def __init__(self, step):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


class WriteDeadline(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, self.t)
        (self.t / "stub.py").write_text(STUB)

    def adapter(self, mode):
        a = SubprocessAdapter([sys.executable, str(self.t / "stub.py")], timeout=5,
                              env=dict(os.environ, MODE=mode), clock=Stepping(1.0))
        self.addCleanup(a.close)
        a.start()
        # The first start reads the hello only; later starts behave.
        a.env = dict(os.environ, MODE="ok")
        return a

    big = b"x" * 4_000_000  # far more than a pipe holds

    def check(self, mode, limits):
        a = self.adapter(mode)
        r = a.run("c", "events", self.big, {}, limits)
        self.assertEqual(r["status"], "timeout", r)
        self.assertIn("did not read its request", r["detail"])
        # The run goes on: the adapter was restarted and the next case is answered.
        again = a.run("c", "events", b"x\n", {})
        self.assertEqual(again["status"], "ok", again)
        self.assertEqual(a.restarts, 1)
        return r

    def test_an_adapter_that_never_reads_times_out(self):
        self.check("none", None)

    def test_an_adapter_that_reads_half_then_stalls_times_out(self):
        self.check("half", None)

    def test_a_bounded_case_uses_its_own_bound(self):
        r = self.check("none", {"time_seconds": 3, "memory_mb": 100})
        self.assertEqual(r["_measure"]["time_bound"], 3)

    def test_the_memory_bound_is_still_enforced_while_the_write_waits(self):
        a = self.adapter("none")
        reads = [0]

        def kb(pids, field):
            reads[0] += 1
            return 0 if reads[0] == 1 else 10 * 1024 * 1024

        with mock.patch.object(P, "tree_kb", kb):
            r = a.run("c", "events", self.big, {}, {"time_seconds": 1000, "memory_mb": 100})
        self.assertEqual(r["status"], "memory", r)

    def test_a_reading_adapter_is_unaffected(self):
        a = self.adapter("ok")
        a.stop()
        a.clock = P.time.monotonic  # a real clock: the reader needs real time to drain the pipe
        a.timeout = 60
        a.start()
        r = a.run("c", "events", self.big, {})
        self.assertEqual(r["status"], "ok", r)


if __name__ == "__main__":
    unittest.main()
