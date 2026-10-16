"""A bounded case's time limit is a hang guard: the runner enforces exactly the number the case states."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from oneyaml_kit import compare
from oneyaml_kit import runner as R
from oneyaml_kit.cases import Case, load_all
from oneyaml_kit.protocol import SubprocessAdapter

FAKE = str(Path(__file__).parent / "fixtures" / "fake_adapter.py")


def bounded_case(bound):
    return Case(id="b", suite="own", category="safety", title="t", origin="bug", op="events",
                input=b"x\n", expect={"kind": "bounded", "time_seconds": bound, "memory_mb": 100,
                                      "accept": {"kind": "events", "events": ["+STR", "-STR"]},
                                      "category": ["limit"]})


class Enforced(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, self.t)

    def run_case(self, behaviour, bound):
        (self.t / "script.json").write_text(json.dumps({"b": behaviour}))
        env = dict(os.environ, FAKE_SCRIPT=str(self.t / "script.json"))
        a = SubprocessAdapter([sys.executable, FAKE], env=env, timeout=30)
        self.addCleanup(a.close)
        h = a.start()
        return R.execute(a, set(h["capabilities"]), [bounded_case(bound)])[0]

    def test_the_limits_are_the_cases_own_numbers(self):
        self.assertEqual(R.limits_of(bounded_case(7.5)), {"time_seconds": 7.5, "memory_mb": 100})

    def test_a_case_that_needs_longer_than_its_bound_fails(self):
        r = self.run_case("slow", 0.5)  # the fake adapter needs a second
        self.assertEqual((r["loose"], r["strict"]), ("fail", "fail"))
        self.assertIn("no answer within 0.5 s", r["detail"])

    def test_the_same_case_passes_with_a_longer_bound_and_records_it(self):
        r = self.run_case("slow", 5)
        self.assertEqual(r["loose"], "pass", r)
        self.assertEqual(r["measured"]["time_bound"], 5)
        self.assertGreater(r["measured"]["seconds"], 0.5)

    def test_the_verdict_judges_the_measured_time_against_the_cases_limit(self):
        e = {"kind": "bounded", "time_seconds": 2, "memory_mb": 100, "accept": None, "category": ["limit"]}
        over = {"status": "error", "error": {"category": "limit"},
                "_measure": {"seconds": 3.0, "peak_mb": 1, "time_bound": 2}}
        inside = {"status": "error", "error": {"category": "limit"},
                  "_measure": {"seconds": 1.5, "peak_mb": 1, "time_bound": 2}}
        c = bounded_case(2)
        self.assertEqual(compare.evaluate(c, e, over, {"events", "error-category"})["loose"], "fail")
        self.assertEqual(compare.evaluate(c, e, inside, {"events", "error-category"})["loose"], "pass")


class Corpus(unittest.TestCase):
    def test_every_bounded_time_limit_is_a_generous_hang_guard(self):
        # The limit is not a speed claim. A case that states less than five seconds would turn a
        # slow or busy machine into a verdict, so the corpus keeps every limit at or above that.
        for c in load_all(include_slow=True):
            if c.expect["kind"] == "bounded":
                self.assertGreaterEqual(c.expect["time_seconds"], 5, c.id)

    def test_a_case_that_can_run_a_library_to_its_full_limit_is_slow(self):
        # A competitor that hangs on this input costs the hand-over run the whole limit.
        ids = {c.id: c for c in load_all(include_slow=True)}
        self.assertIn("slow", ids["own:safety/deep-flow-mappings-999999"].tags)


if __name__ == "__main__":
    unittest.main()
