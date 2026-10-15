"""The two 64 MiB early-error cases: what they build, how their bounds are derived, and that they bite."""

import unittest

import support
from oneyaml_kit import cases as C
from oneyaml_kit.runner import execute

BRACKETS = "own:safety/early-error-open-brackets-64-mebibytes"
FIRST_LINE = "own:safety/early-error-in-first-line-then-64-mebibytes"
TEN_MILLION_DIGITS = "own:safety/ten-million-digit-plain-scalar-events"
MIB = 1 << 20
CASES = {c.id: c for c in C.load_own(include_slow=True)}


class Peak(support.OracleAdapter):
    """An oracle that reports a chosen peak memory growth, and optionally a chosen answer."""

    def __init__(self, cases, peak_mb, answer=None):
        super().__init__(cases)
        self.peak_mb, self.answer = peak_mb, answer

    def run(self, case_id, op, data, args, limits=None):
        r = self.answer(case_id) if self.answer else self._answer(case_id, op, data, args)
        if limits:
            r["_measure"] = {"seconds": 0.1, "peak_mb": self.peak_mb}
        return r


def verdicts(case_id, peak_mb, answer=None):
    c = CASES[case_id]
    r = execute(Peak([c], peak_mb, answer), set(support.ALL_CAPS), [c])[0]
    return r["loose"], r["strict"]


class Shape(unittest.TestCase):
    def test_the_inputs_are_what_the_notes_say(self):
        self.assertEqual(CASES[BRACKETS].input, b"[" * (64 * MIB))
        data = CASES[FIRST_LINE].input
        self.assertTrue(data.startswith(b"a: b: c\nx:\nx:\n"))
        self.assertEqual(len(data), 8 + 3 * 22_369_618)
        self.assertLessEqual(len(data), 64 * MIB)
        self.assertGreater(len(data), 64 * MIB - 8)
        self.assertEqual(data.count(b"\n"), 22_369_619)

    def test_both_are_slow_events_cases_that_allow_no_result(self):
        for cid, errors in ((BRACKETS, ["limit", "syntax"]), (FIRST_LINE, ["syntax", "limit"])):
            c = CASES[cid]
            self.assertIn("slow", c.tags)
            self.assertEqual((c.category, c.op), ("safety", "events"))
            self.assertEqual(c.expect["kind"], "bounded")
            self.assertIsNone(c.expect["accept"])
            self.assertEqual(c.expect["category"], errors)

    def test_the_bounds_follow_from_the_input_and_the_sibling(self):
        # Memory: 24 times the input, 1.46 times the heaviest delivery cost measured (about 16.4 times in
        # a runtime with UTF-16 strings). Time: the sibling's 0.2 s per megabyte over 64 MiB, to the second.
        rate = CASES[TEN_MILLION_DIGITS].expect["time_seconds"] / 10.0
        self.assertAlmostEqual(rate, 0.2)
        for cid in (BRACKETS, FIRST_LINE):
            e = CASES[cid].expect
            self.assertEqual(e["memory_mb"], 24 * 64)
            self.assertLessEqual(e["time_seconds"], 2 * rate * 64 * MIB / 1e6)
            self.assertGreaterEqual(e["time_seconds"], rate * 64 * MIB / 1e6)


class Bites(unittest.TestCase):
    def test_an_error_inside_the_bounds_passes(self):
        for cid in (BRACKETS, FIRST_LINE):
            self.assertEqual(verdicts(cid, 330.0), ("pass", "pass"))
            self.assertEqual(verdicts(cid, 1050.0), ("pass", "pass"), "the heaviest delivery cost measured")

    def test_memory_in_proportion_to_the_input_fails(self):
        for cid in (BRACKETS, FIRST_LINE):
            self.assertEqual(verdicts(cid, 2451.0), ("fail", "fail"))

    def test_an_answer_fails_and_so_does_the_wrong_kind_of_error(self):
        ok = lambda cid: {"id": cid, "status": "ok", "events": ["+STR", "-STR"]}
        wrong = lambda cid: {"id": cid, "status": "error", "error": {"category": "encoding", "message": "x"}}
        for cid in (BRACKETS, FIRST_LINE):
            self.assertEqual(verdicts(cid, 300.0, ok), ("fail", "fail"))
            self.assertEqual(verdicts(cid, 300.0, wrong), ("pass", "fail"))


if __name__ == "__main__":
    unittest.main()
