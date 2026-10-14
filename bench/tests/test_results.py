"""Self-tests for the benchmark logic: ratios, verdicts, rows, tallies, publishability,
result validation and the known-deviation check. Each rule is pinned by a case that
fails if the rule is changed."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneyaml_bench import results as R  # noqa: E402


def cell(lib, role="competitor", task="generic-load", workload="w", status="ok", t=100.0, mad=1.0,
         peak=1000, language="rust", variant="spec", detail=""):
    c = {"language": language, "library": lib, "role": role, "task": task, "workload": workload,
         "status": status, "input_bytes": 10}
    if status == "ok":
        c["variant"] = variant
        c["time"] = {"median_ns": t, "mad_ns": mad, "samples": 5, "min_ns": t, "max_ns": t}
        c["memory"] = {"method": "test", "peak_bytes": peak, "allocated_bytes": peak * 2, "allocations": 3}
    else:
        c["detail"] = detail or ("unsupported" if status == "unsupported" else "why")
    return c


class RatioAndVerdict(unittest.TestCase):
    def test_ratio_is_value_over_reference(self):
        self.assertEqual(R.ratio(150.0, 100.0), 1.5)
        self.assertEqual(R.ratio(50.0, 100.0), 0.5)
        with self.assertRaises(R.ResultError):
            R.ratio(1.0, 0.0)

    def test_time_verdict_thresholds(self):
        # Lower is better. Spread 0 leaves only the 2% floor.
        self.assertEqual(R.time_verdict(97.0, 0, 100.0, 0), "win")
        self.assertEqual(R.time_verdict(103.0, 0, 100.0, 0), "loss")
        self.assertEqual(R.time_verdict(101.0, 0, 100.0, 0), "tie")
        self.assertEqual(R.time_verdict(99.0, 0, 100.0, 0), "tie")

    def test_time_verdict_widens_with_noise(self):
        # 10% apart is a win when quiet, a tie when the spread is 5% (3 x 5% = 15%).
        self.assertEqual(R.time_verdict(90.0, 1.0, 100.0, 1.0), "win")
        self.assertEqual(R.time_verdict(90.0, 4.5, 100.0, 5.0), "tie")
        # The noisier of the two sides decides.
        self.assertEqual(R.time_verdict(90.0, 0.0, 100.0, 5.0), "tie")

    def test_memory_verdict_is_nearly_exact(self):
        self.assertEqual(R.memory_verdict(98, 100), "win")
        self.assertEqual(R.memory_verdict(102, 100), "loss")
        self.assertEqual(R.memory_verdict(1000, 1005), "tie")


class Rows(unittest.TestCase):
    def test_reference_is_the_best_competitor_not_the_first_or_ours(self):
        cells = [cell("ours", "own", t=120), cell("slow", t=300), cell("fast", t=100), cell("mid", t=110)]
        [row] = R.compare_rows(cells)
        self.assertEqual(row["best_time"]["library"], "fast")
        e = row["own"][0]["time"]
        self.assertEqual(e["vs"], "fast")
        self.assertAlmostEqual(e["ratio"], 1.2)
        self.assertEqual(e["verdict"], "loss")

    def test_a_win_and_a_tie(self):
        [row] = R.compare_rows([cell("ours", "own", t=50), cell("rival", t=100)])
        self.assertEqual(row["own"][0]["time"]["verdict"], "win")
        [row] = R.compare_rows([cell("ours", "own", t=100.5), cell("rival", t=100)])
        self.assertEqual(row["own"][0]["time"]["verdict"], "tie")

    def test_a_wrong_competitor_is_not_the_reference(self):
        # The fastest "competitor" gave a wrong answer, so it has no number to compare with.
        cells = [cell("ours", "own", t=100), cell("fake", status="differs", detail="x"), cell("real", t=200)]
        [row] = R.compare_rows(cells)
        self.assertEqual(row["best_time"]["library"], "real")
        self.assertEqual(row["own"][0]["time"]["verdict"], "win")

    def test_no_competitor_means_no_rival(self):
        [row] = R.compare_rows([cell("ours", "own"), cell("rival", status="unsupported")])
        self.assertIsNone(row["own"][0]["time"])
        self.assertEqual(R.tally([row], "time")["no-rival"], 1)

    def test_our_wrong_answer_has_no_number(self):
        [row] = R.compare_rows([cell("ours", "own", status="differs"), cell("rival")])
        self.assertEqual(row["own"][0]["status"], "differs")
        self.assertIsNone(row["own"][0]["time"])
        self.assertEqual(R.tally([row], "time")["no-number"], 1)

    def test_languages_never_share_a_row(self):
        cells = [cell("ours", "own", language="rust", t=10), cell("rival", language="rust", t=20),
                 cell("go-ours", "own", language="go", t=1000), cell("go-rival", language="go", t=900)]
        rows = R.compare_rows(cells)
        self.assertEqual(len(rows), 2)
        by = {r["cells"][0]["language"]: r for r in rows}
        self.assertEqual(by["rust"]["own"][0]["time"]["verdict"], "win")
        self.assertEqual(by["go"]["own"][0]["time"]["verdict"], "loss")

    def test_memory_has_its_own_best_competitor(self):
        cells = [cell("ours", "own", t=100, peak=500), cell("fast", t=50, peak=2000), cell("lean", t=400, peak=250)]
        [row] = R.compare_rows(cells)
        self.assertEqual(row["own"][0]["time"]["vs"], "fast")
        self.assertEqual(row["own"][0]["memory"]["vs"], "lean")
        self.assertEqual(row["own"][0]["memory"]["verdict"], "loss")

    def test_controls_never_become_a_reference(self):
        cells = [cell("ours", "own", t=100), cell("control-correct", "control", t=1), cell("rival", t=90)]
        [row] = R.compare_rows(cells)
        self.assertEqual(row["best_time"]["library"], "rival")


class Publishability(unittest.TestCase):
    QUIET = [0.1] * 20

    def assess(self, **kw):
        args = dict(mode="full", samples=self.QUIET, load1_start=1.0, load1_end=1.2, complete=True,
                    pinned_cpu=3, governor="performance")
        args.update(kw)
        return R.assess_publishability(**args)

    def test_a_quiet_complete_full_run_is_publishable(self):
        a = self.assess()
        self.assertTrue(a["publishable"])
        self.assertEqual((a["label"], a["reasons"]), ("publishable", []))

    def test_each_condition_alone_blocks(self):
        cases = {
            "mode": dict(mode="quick"),
            "complete": dict(complete=False),
            "selftest": dict(selftest_build=True),
            "few samples": dict(samples=[0.0] * 4),
            "p95": dict(samples=[0.1] * 10 + [2.0] * 10),
            "worst sample": dict(samples=[0.1] * 99 + [5.0]),
            "load at start": dict(load1_start=3.5),
            "load at end": dict(load1_end=3.5),
            "not pinned": dict(pinned_cpu=None),
            "powersave governor": dict(governor="powersave"),
            "unknown governor": dict(governor="unknown"),
        }
        for name, kw in cases.items():
            a = self.assess(**kw)
            self.assertFalse(a["publishable"], name)
            self.assertEqual(len(a["reasons"]), 1, f"{name}: {a['reasons']}")
            self.assertEqual(a["label"], "smoke")

    def test_thresholds_are_inclusive(self):
        self.assertTrue(self.assess(samples=[1.0] * 20)["publishable"])
        self.assertTrue(self.assess(load1_start=3.0, load1_end=3.0)["publishable"])
        self.assertTrue(self.assess(samples=[0.1] * 19 + [4.0])["publishable"])

    def test_the_measured_values_and_the_rule_are_recorded(self):
        a = self.assess(samples=[0.5] * 10)
        self.assertEqual(a["measured"]["foreign_busy_cores_max"], 0.5)
        self.assertEqual(a["rule"], R.PUBLISH_RULE)

    def test_pinning_and_governor_are_stated_in_the_reasons_and_recorded(self):
        a = self.assess(pinned_cpu=None, governor="powersave")
        self.assertEqual(len(a["reasons"]), 2)
        self.assertTrue(any("not pinned" in r for r in a["reasons"]))
        self.assertTrue(any("'powersave'" in r for r in a["reasons"]))
        self.assertEqual((a["measured"]["pinned_cpu"], a["measured"]["governor"]), (None, "powersave"))

    def test_reasons_found_after_the_run_make_it_a_smoke_result(self):
        r = result([cell("a")], mode="full", publishable=True, reasons=())
        R.add_reasons(r, [])
        self.assertTrue(r["run"]["publishability"]["publishable"])
        R.add_reasons(r, ["the list of known deviations does not match"])
        pub = r["run"]["publishability"]
        self.assertEqual((pub["publishable"], pub["label"]), (False, "smoke"))
        self.assertEqual(pub["reasons"], ["the list of known deviations does not match"])
        R.validate(r)

    def test_percentile(self):
        self.assertEqual(R.percentile([1, 2, 3, 4, 5], 0.5), 3)
        self.assertEqual(R.percentile([], 0.9), 0.0)
        self.assertAlmostEqual(R.percentile(list(range(101)), 0.95), 95)


def result(cells, mode="quick", publishable=False, reasons=("quick",)):
    return {"format": R.FORMAT, "harnesses": {}, "workloads": [],
            "run": {"date": "d", "mode": mode, "environment": {},
                    "publishability": {"publishable": publishable, "reasons": list(reasons)}},
            "cells": cells}


class Validation(unittest.TestCase):
    def test_a_good_result_passes(self):
        R.validate(result([cell("a", "own"), cell("b", status="differs"), cell("c", status="unsupported")]))

    def test_a_wrong_answer_may_not_carry_a_number(self):
        bad = cell("a", "own", status="differs")
        bad["time"] = {"median_ns": 1.0, "mad_ns": 0, "samples": 1}
        with self.assertRaisesRegex(R.ResultError, "carries numbers"):
            R.validate(result([bad]))

    def test_an_ok_cell_needs_both_figures(self):
        bad = cell("a", "own")
        del bad["memory"]
        with self.assertRaisesRegex(R.ResultError, "lacks memory"):
            R.validate(result([bad]))

    def test_a_failure_needs_a_reason(self):
        bad = cell("a", status="rejects")
        bad["detail"] = ""
        with self.assertRaisesRegex(R.ResultError, "without a reason"):
            R.validate(result([bad]))

    def test_duplicates_and_bad_statuses_are_refused(self):
        with self.assertRaisesRegex(R.ResultError, "duplicate"):
            R.validate(result([cell("a"), cell("a")]))
        with self.assertRaisesRegex(R.ResultError, "bad status"):
            R.validate(result([cell("a", status="maybe")]))

    def test_publishable_needs_a_full_run_without_reasons(self):
        with self.assertRaisesRegex(R.ResultError, "cannot be publishable"):
            R.validate(result([cell("a")], mode="quick", publishable=True, reasons=()))
        with self.assertRaisesRegex(R.ResultError, "cannot be publishable"):
            R.validate(result([cell("a")], mode="full", publishable=True, reasons=("busy",)))
        R.validate(result([cell("a")], mode="full", publishable=True, reasons=()))

    def test_an_unjudged_cell_has_no_numbers_and_needs_a_reason(self):
        R.validate(result([cell("a", status="unjudged", detail="the referees disagree")]))
        bad = cell("a", status="unjudged", detail="x")
        bad["time"] = {"median_ns": 1.0, "mad_ns": 0, "samples": 1}
        with self.assertRaisesRegex(R.ResultError, "carries numbers"):
            R.validate(result([bad]))
        silent = cell("a", status="unjudged")
        silent["detail"] = ""
        with self.assertRaisesRegex(R.ResultError, "without a reason"):
            R.validate(result([silent]))

    def test_the_memory_method_is_required(self):
        bad = cell("a", "own")
        del bad["memory"]["method"]
        with self.assertRaisesRegex(R.ResultError, "lacks memory"):
            R.validate(result([bad]))

    def test_wrong_format_is_refused(self):
        r = result([])
        r["format"] = "other/1"
        with self.assertRaises(R.ResultError):
            R.validate(r)


class KnownDeviations(unittest.TestCase):
    def known(self, *entries):
        return {"entries": [dict(e, reason=e.get("reason", "because")) for e in entries]}

    def test_a_listed_failure_passes(self):
        cells = [cell("ours", "own", status="differs", workload="x")]
        k = self.known({"cell": "ours/generic-load/x", "status": "differs"})
        self.assertEqual(R.verify_known(cells, k, "rust"), [])

    def test_an_unlisted_failure_fails(self):
        cells = [cell("ours", "own", status="differs", workload="x")]
        [p] = R.verify_known(cells, self.known(), "rust")
        self.assertIn("unlisted deviation ours/generic-load/x", p)

    def test_a_stale_entry_fails(self):
        cells = [cell("ours", "own", workload="x")]
        k = self.known({"cell": "ours/generic-load/x", "status": "differs"})
        [p] = R.verify_known(cells, k, "rust")
        self.assertIn("stale", p)

    def test_a_changed_status_fails(self):
        cells = [cell("ours", "own", status="rejects", workload="x")]
        k = self.known({"cell": "ours/generic-load/x", "status": "differs"})
        [p] = R.verify_known(cells, k, "rust")
        self.assertIn("listed as differs", p)

    def test_variants_are_deviations(self):
        cells = [cell("ours", "own", workload="x", variant="merged")]
        self.assertEqual(len(R.verify_known(cells, self.known(), "rust")), 1)
        k = self.known({"cell": "ours/generic-load/x", "status": "ok", "variant": "merged"})
        self.assertEqual(R.verify_known(cells, k, "rust"), [])

    def test_unsupported_and_unmeasured_cells_are_not_deviations(self):
        cells = [cell("ours", "own", status="unsupported", workload="x")]
        k = self.known({"cell": "ours/generic-load/not-run", "status": "differs"})
        self.assertEqual(R.verify_known(cells, k, "rust"), [])

    def test_a_document_judged_on_bytes_alone_is_a_listed_deviation(self):
        cells = [cell("edit", workload="x", variant="bytes-only")]
        self.assertEqual(len(R.verify_known(cells, self.known(), "rust")), 1)
        k = self.known({"cell": "edit/generic-load/x", "status": "ok", "variant": "bytes-only"})
        self.assertEqual(R.verify_known(cells, k, "rust"), [])

    UNIVERSE = {"x": ["generic-load", "dump"], "y": ["generic-load"]}
    LIBS = {"ours", "rival"}

    def check(self, *entries, cells=()):
        return R.verify_known(list(cells), self.known(*entries), "rust", self.UNIVERSE, self.LIBS)

    def test_entries_for_unknown_libraries_tasks_and_workloads_are_stale_even_if_unmeasured(self):
        [p] = self.check({"cell": "ghost/generic-load/x", "status": "differs"})
        self.assertIn("no library 'ghost'", p)
        [p] = self.check({"cell": "ours/generic-load/nowhere", "status": "differs"})
        self.assertIn("no workload 'nowhere'", p)
        [p] = self.check({"cell": "ours/edit-script/x", "status": "differs"})
        self.assertIn("has no task 'edit-script'", p)
        [p] = self.check({"cell": "ours/dump/y", "status": "differs"})
        self.assertIn("has no task 'dump'", p)
        [p] = self.check({"cell": "ours-generic-load-x", "status": "differs"})
        self.assertIn("not library/task/workload", p)

    def test_a_real_cell_that_was_not_measured_is_left_alone(self):
        self.assertEqual(self.check({"cell": "ours/dump/x", "status": "differs"}), [])
        measured = [cell("ours", "own", workload="y")]
        self.assertEqual(self.check({"cell": "ours/dump/x", "status": "differs"}, cells=measured), [])

    def test_a_duplicate_entry_is_refused(self):
        e = {"cell": "ours/dump/x", "status": "differs"}
        self.assertEqual(len(self.check(e, e)), 1)

    def test_an_entry_needs_a_reason(self):
        k = {"entries": [{"cell": "a/b/c", "status": "differs", "reason": ""}]}
        self.assertEqual(len(R.verify_known([], k, "rust")), 1)


if __name__ == "__main__":
    unittest.main()
