"""The time-bound factor: a slow machine stretches a case's time bound, a fast one never shrinks it."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401
from oneyaml_kit import runner as R
from oneyaml_kit import speed
from oneyaml_kit.cases import Case
from oneyaml_kit.protocol import SubprocessAdapter

FAKE = str(Path(__file__).parent / "fixtures" / "fake_adapter.py")


class Factor(unittest.TestCase):
    def test_the_factor_is_never_below_one_and_moves_in_steps(self):
        ref = speed.REFERENCE_SECONDS
        self.assertEqual(speed.factor_for(0.0), 1.0)
        self.assertEqual(speed.factor_for(ref), 1.0)
        self.assertEqual(speed.factor_for(ref * 1.01), 2.0)
        self.assertEqual(speed.factor_for(ref * 3), 4.0)
        self.assertEqual(speed.factor_for(ref * 5), 8.0)
        # A machine slower than the largest step is still held to the largest one.
        self.assertEqual(speed.factor_for(ref * 1000), 8.0)

    def test_a_fixed_factor_skips_the_measurement(self):
        with mock.patch.dict(os.environ, {"KIT_TIME_SCALE": "3"}):
            with mock.patch.object(speed, "probe", side_effect=AssertionError("probed")):
                self.assertEqual(speed.time_scale(), 3.0)
        with mock.patch.dict(os.environ, {"KIT_TIME_SCALE": "0"}):
            with self.assertRaises(ValueError):
                speed.time_scale()

    def test_the_measured_factor_follows_the_probe_and_is_reused_briefly(self):
        env = {k: v for k, v in os.environ.items() if k != "KIT_TIME_SCALE"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(speed, "_cache", None):
                with mock.patch.object(speed, "probe", return_value=speed.REFERENCE_SECONDS * 3) as p:
                    self.assertEqual(speed.time_scale(), 4.0)
                    self.assertEqual(speed.time_scale(), 4.0)
                    self.assertEqual(p.call_count, 1)

    def test_the_real_probe_is_a_plain_positive_time(self):
        self.assertGreater(speed.probe(), 0)


class Stretching(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, self.t)

    def run_slow(self, scale, bound=0.5):
        (self.t / "script.json").write_text(json.dumps({"b": "slow"}))
        env = dict(os.environ, FAKE_SCRIPT=str(self.t / "script.json"), KIT_TIME_SCALE=str(scale))
        a = SubprocessAdapter([sys.executable, FAKE], env=env, timeout=30)
        self.addCleanup(a.close)
        h = a.start()
        case = Case(id="b", suite="own", category="safety", title="t", origin="bug", op="events",
                    input=b"x\n", expect={"kind": "bounded", "time_seconds": bound, "memory_mb": 100,
                                          "accept": {"kind": "events", "events": ["+STR", "-STR"]},
                                          "category": ["limit"]})
        with mock.patch.dict(os.environ, {"KIT_TIME_SCALE": str(scale)}):
            return R.execute(a, set(h["capabilities"]), [case])[0]

    def test_a_case_that_needs_longer_than_its_bound_fails_on_the_reference_machine(self):
        r = self.run_slow(1)
        self.assertEqual((r["loose"], r["strict"]), ("fail", "fail"))
        self.assertIn("no answer within 0.5 s", r["detail"])

    def test_the_same_case_passes_where_the_bound_is_stretched_and_says_which_bound_held(self):
        r = self.run_slow(4)
        self.assertEqual(r["loose"], "pass", r)
        self.assertEqual(r["measured"]["time_bound"], 2.0)
        self.assertGreater(r["measured"]["seconds"], 0.5)

    def test_a_stretched_bound_still_stops_a_case_that_runs_past_it(self):
        r = self.run_slow(1.5, bound=0.4)  # 0.6 s allowed, the adapter needs a second
        self.assertEqual((r["loose"], r["strict"]), ("fail", "fail"))
        self.assertIn("no answer within 0.6 s", r["detail"])

    def test_the_bound_in_the_verdict_is_the_one_that_was_enforced(self):
        # A measurement over the stretched bound fails; one inside it passes, whatever the case says.
        from oneyaml_kit import compare
        e = {"kind": "bounded", "time_seconds": 1, "memory_mb": 100, "accept": None, "category": ["limit"]}
        over = {"status": "error", "error": {"category": "limit"},
                "_measure": {"seconds": 3.0, "peak_mb": 1, "time_bound": 2.0}}
        inside = {"status": "error", "error": {"category": "limit"},
                  "_measure": {"seconds": 1.5, "peak_mb": 1, "time_bound": 2.0}}
        c = Case(id="b", suite="own", category="safety", title="t", origin="bug", op="events",
                 input=b"x\n", expect=e)
        self.assertEqual(compare.evaluate(c, e, over, {"events", "error-category"})["loose"], "fail")
        self.assertEqual(compare.evaluate(c, e, inside, {"events", "error-category"})["loose"], "pass")


if __name__ == "__main__":
    unittest.main()
