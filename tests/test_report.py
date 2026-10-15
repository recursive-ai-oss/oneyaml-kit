import os
import shutil
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from oneyaml_kit import report
from oneyaml_kit.public_text import scan

GOLDEN = Path(__file__).parent / "fixtures" / "report_golden.md"


def tmp():
    return Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))


def case(i, suite, op, expect, loose, strict, corrected=None, category="parse"):
    r = {"id": i, "suite": suite, "category": category, "op": op, "expect": expect,
         "loose": loose, "strict": strict}
    if corrected:
        r["corrected"] = corrected
    return r


def sample():
    cases = [
        case("yts:A", "yaml-test-suite", "events", "events", "pass", "pass"),
        case("yts:B", "yaml-test-suite", "events", "error", "pass", "fail",
             corrected={"loose": "pass", "strict": "pass", "detail": ""}),
        case("yts:C", "yaml-test-suite", "events", "error", "pass", "pass"),
        case("own:edit/x", "own", "patch", "bytes", "fail", "fail", category="edit"),
        case("own:edit/y", "own", "patch", "bytes", "unsupported", "unsupported", category="edit"),
        dict(case("own:edit/z", "own", "patch", "error", "pass", "pass", category="edit"), emulated=["unchanged"]),
        case("own:diagnostics/p", "own", "events", "error", "pass", "unsupported", category="diagnostics"),
    ]
    return {
        "format": "oneyaml-kit-results/1",
        "kit": {"version": "9.9.9", "corpus_digest": "0123456789abcdef" * 4,
                "suites": {"yaml-test-suite": {"release": "data-2022-01-17",
                                               "revision": "6e6c296ae9c9d2d5c4134b4b64d01b29ac19ff6f"},
                           "own": {"cases": 2}}},
        "run": {"date": "2030-01-02", "mode": "fast", "complete": True, "cases": 7, "seconds": 0.5},
        "adapter": {"name": "lib", "version": "1.2.3", "language": "go", "capabilities": [], "protocol": 1,
                    "emulates": ["unchanged"]},
        "cases": cases,
        "deviations": {"listed": 1, "unlisted": [], "stale": [], "unknown": [], "ok": True,
                       "entries": [{"case": "yts:B", "expectation": "primary", "mode": "strict",
                                    "verdict": "fail", "reason": "events differ | with a pipe"}]},
    }


class Report(unittest.TestCase):
    def test_golden_output(self):
        md = report.render([sample()])
        self.assertEqual(md, GOLDEN.read_text(encoding="utf-8"))

    def test_counts(self):
        md = report.render([sample()])
        self.assertIn("| yaml-test-suite · events · invalid input | 2 | 2/2 | 1/2 | 2/2 | 0 | 0 | 0 |", md)
        self.assertIn("| yaml-test-suite · events · valid input | 1 | 1/1 | 1/1 | – | 0 | 0 | 0 |", md)
        # One edit case is unsupported, one failed, one passed only through adapter emulation.
        self.assertIn("| kit cases · edit · edits by patch | 3 | 1/3 | 1/3 | – | 1 | 0 | 1 |", md)
        # Strict-only unsupported is shown apart from unsupported.
        self.assertIn("| kit cases · diagnostics · events | 1 | 1/1 | 0/1 | – | 0 | 1 | 0 |", md)
        self.assertIn("Behaviours this adapter supplies itself", md)
        self.assertIn("Scored baseline: yaml-test-suite `data-2022-01-17`", md)
        self.assertNotIn("differ | with", md)

    def test_convention_cases_are_counted_apart(self):
        res = sample()
        res["cases"] = res["cases"] + [
            dict(case("own:edit/c1", "own", "patch", "bytes", "pass", "pass", category="edit"), tags=["convention"]),
            dict(case("own:edit/c2", "own", "patch", "bytes", "fail", "fail", category="edit"),
                 tags=["convention", "comment-ownership"]),
            dict(case("own:edit/c3", "own", "patch", "bytes", "unsupported", "unsupported", category="edit"),
                 tags=["convention"]),
        ]
        md = report.render([res])
        self.assertIn("rest on a kit convention", md)
        self.assertIn("| kit cases · edit · edits by patch | 3 | 1 | 1 | 1 | 1 | 1 |", md)
        # With none tagged there is no such section.
        self.assertNotIn("rest on a kit convention (", report.render([sample()]))
        self.assertNotIn("Cases whose expected bytes rest on a kit convention", report.render([sample()]))

    def test_written_values_are_a_group_of_their_own_and_a_convention(self):
        res = sample()
        res["cases"] = res["cases"] + [
            dict(case("own:emit/a", "own", "dump", "readback", "pass", "pass", category="emit"), tags=["convention"]),
            dict(case("own:emit/b", "own", "dump", "readback", "pass", "fail", category="emit"), tags=["convention"]),
            dict(case("own:emit/c", "own", "dump", "readback", "fail", "fail", category="emit"), tags=["convention"]),
            dict(case("own:emit/d", "own", "dump", "readback", "unsupported", "unsupported", category="emit"),
                 tags=["convention"]),
        ]
        md = report.render([res])
        self.assertIn("| kit cases · emit · values written | 4 | 2/4 | 1/4 | – | 1 | 0 | 0 |", md)
        # Loose is the Core reading, strict adds the family convention; both are explained.
        self.assertIn("| kit cases · emit · values written | 4 | 2 | 1 | 1 | 2 | 1 |", md)
        self.assertIn("Values written", md)

    def test_generated_text_passes_the_public_text_check(self):
        t = tmp()
        try:
            f = t / "r.md"
            f.write_text(report.render([sample()]), encoding="utf-8")
            self.assertEqual(scan([f], root=t), [])
        finally:
            shutil.rmtree(t)

    def test_no_results_is_an_error(self):
        with self.assertRaises(ValueError):
            report.render([])


if __name__ == "__main__":
    unittest.main()
