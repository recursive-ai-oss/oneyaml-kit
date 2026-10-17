"""The driver's hang guard and its cleanup of harness processes, against stub harnesses (no library).

The stub speaks the harness protocol: a `begin` mark, then a `cell` record, per cell. The limits used
here are tiny and the stubs sleep for far longer, so no test depends on how long anything takes.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneyaml_bench import results, run  # noqa: E402

STUB = textwrap.dedent('''
    import json, os, subprocess, sys, time
    args = sys.argv[1:]
    out = args[args.index("--out") + 1]
    skip = [args[i + 1] for i, a in enumerate(args) if a == "--skip"]
    # STUB_HANG names the cell that never finishes; it also starts a child that would outlive it.
    hang = os.environ.get("STUB_HANG", "")
    pidfile = os.environ.get("STUB_PIDFILE", "")
    leave_child = os.environ.get("STUB_LEAVE_CHILD") == "1"
    with open(out, "w") as f:
        f.write(json.dumps({"type": "harness", "name": "stub", "libraries": []}) + "\\n")
        f.flush()
        for name in ["lib/t/one", "lib/t/two", "lib/t/three"]:
            if name in skip:
                continue
            f.write(json.dumps({"type": "begin", "cell": name}) + "\\n")
            f.flush()
            if name == hang:
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
                if pidfile:
                    open(pidfile, "w").write(str(child.pid))
                time.sleep(3600)
            lib, task, wl = name.split("/")
            f.write(json.dumps({"type": "cell", "library": lib, "task": task, "workload": wl,
                                "status": "ok"}) + "\\n")
            f.flush()
    if leave_child:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
        open(pidfile, "w").write(str(child.pid))
''')


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def gone(pid: int) -> bool:
    """A killed process takes a moment to disappear; wait for it, with a generous deadline."""
    for _ in range(200):
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


class Stub(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "stub.py").write_text(STUB, encoding="utf-8")
        self.h = {"language": "stub", "_dir": self.dir, "command": [sys.executable, str(self.dir / "stub.py")]}
        self.pidfile = self.dir / "child.pid"
        self.addCleanup(self.reap)

    def tearDown(self):
        self.tmp.cleanup()

    def reap(self):
        if self.pidfile.exists():
            try:
                os.kill(int(self.pidfile.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass

    def env(self, **kv):
        old = {k: os.environ.get(k) for k in kv}
        os.environ.update(kv)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in old.items()])


class HangGuard(Stub):
    def test_a_cell_past_the_limit_is_an_error_and_the_run_goes_on(self):
        self.env(STUB_HANG="lib/t/two", STUB_PIDFILE=str(self.pidfile))
        _, cells = run.run_harness(self.h, self.dir, self.dir, "quick", self.dir, log=lambda *_: None,
                                   cell_limit=1.0)
        by = {f"{c['library']}/{c['task']}/{c['workload']}": c for c in cells}
        self.assertEqual(sorted(by), ["lib/t/one", "lib/t/three", "lib/t/two"])
        self.assertEqual([by[k]["status"] for k in sorted(by)], ["ok", "ok", "error"])
        self.assertIn("limit", by["lib/t/two"]["detail"])
        self.assertTrue(gone(int(self.pidfile.read_text())), "the hung cell's child was left running")

    def test_the_error_cell_is_a_valid_non_number(self):
        self.env(STUB_HANG="lib/t/one")
        _, cells = run.run_harness(self.h, self.dir, self.dir, "quick", self.dir, log=lambda *_: None,
                                   cell_limit=1.0)
        cell = next(c for c in cells if c["status"] == "error")
        self.assertIn(cell["status"], results.STATUSES)
        self.assertTrue(cell["detail"])
        self.assertNotIn("median", cell)

    def test_a_hang_before_any_cell_is_reported_not_retried_forever(self):
        (self.dir / "stub.py").write_text("import time; time.sleep(3600)\n", encoding="utf-8")
        with self.assertRaises(run.HarnessError) as e:
            run.run_harness(self.h, self.dir, self.dir, "quick", self.dir, log=lambda *_: None, cell_limit=1.0)
        self.assertIn("limit", str(e.exception))

    def test_the_limit_is_recorded_in_the_result(self):
        rec = run.assemble("quick", True, {"stub": {"libraries": []}}, {"stub": []}, [], 0.0, 0.0, 0.0,
                           cell_limit=run.CELL_LIMIT_SECONDS)
        self.assertEqual(rec["run"]["cell_limit_seconds"], run.CELL_LIMIT_SECONDS)


class Cleanup(Stub):
    def test_a_child_left_by_a_harness_that_exits_is_killed(self):
        self.env(STUB_LEAVE_CHILD="1", STUB_PIDFILE=str(self.pidfile))
        run.run_harness(self.h, self.dir, self.dir, "quick", self.dir, log=lambda *_: None)
        self.assertTrue(gone(int(self.pidfile.read_text())))

    def driver_killed_by(self, sig):
        code = ("import sys; from pathlib import Path; sys.path.insert(0, %r)\n"
                "from oneyaml_bench import run\n"
                "h = {'language': 'stub', '_dir': Path(%r), 'command': [sys.executable, %r]}\n"
                "run.run_harness(h, Path(%r), Path(%r), 'quick', Path(%r), log=lambda *_: None)\n"
                % (str(Path(run.__file__).parents[1]), str(self.dir), str(self.dir / "stub.py"),
                   str(self.dir), str(self.dir), str(self.dir)))
        env = dict(os.environ, STUB_HANG="lib/t/two", STUB_PIDFILE=str(self.pidfile))
        driver = subprocess.Popen([sys.executable, "-c", code], env=env, stderr=subprocess.DEVNULL,
                                  # As when started by nohup or a shell `&`: SIGINT arrives ignored.
                                  preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
        self.addCleanup(lambda: driver.poll() is None and driver.kill())
        # Wait for the stub's child to exist: that is the moment a cell is in flight.
        while not (self.pidfile.exists() and self.pidfile.read_text()):
            self.assertIsNone(driver.poll(), "the driver ended before the cell started")
            time.sleep(0.05)
        child = int(self.pidfile.read_text())
        self.assertTrue(alive(child))
        driver.send_signal(sig)
        driver.wait()
        self.assertTrue(gone(child), "a harness child outlived the driver")

    def test_sigterm_leaves_no_process(self):
        self.driver_killed_by(signal.SIGTERM)

    def test_sigint_leaves_no_process(self):
        self.driver_killed_by(signal.SIGINT)


if __name__ == "__main__":
    unittest.main()
