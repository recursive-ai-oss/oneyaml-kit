"""Self-tests for the benchmark logic: ratios, verdicts, rows, tallies and result validation.
Each rule is pinned by a case that fails if the rule is changed."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneyaml_bench import results as R  # noqa: E402


def cell(lib, role="competitor", task="generic-load", workload="w", status="ok", t=100.0,
         peak=1000, language="rust", variant="spec", detail=""):
    c = {"language": language, "library": lib, "role": role, "task": task, "workload": workload,
         "status": status, "input_bytes": 10}
    if status == "ok":
        c["variant"] = variant
        c["time"] = {"ns": t, "stat": "mean"}
        c["memory"] = {"allocated_bytes": peak, "allocations": 3}
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
        # Lower is better; a gap under 5% is a tie.
        self.assertEqual(R.verdict(90.0, 100.0, R.TIME_BAND), "win")
        self.assertEqual(R.verdict(110.0, 100.0, R.TIME_BAND), "loss")
        self.assertEqual(R.verdict(103.0, 100.0, R.TIME_BAND), "tie")
        self.assertEqual(R.verdict(97.0, 100.0, R.TIME_BAND), "tie")

    def test_memory_verdict_is_nearly_exact(self):
        self.assertEqual(R.verdict(95, 100, R.MEMORY_BAND), "win")
        self.assertEqual(R.verdict(105, 100, R.MEMORY_BAND), "loss")
        self.assertEqual(R.verdict(1000, 1010, R.MEMORY_BAND), "tie")

    def test_wider_band_applies_to_a_comparison_with_yamldotnet(self):
        # YamlDotNet's allocation figure drifts between runs, so a gap inside the wider band is a tie.
        band = R.memory_band("oneyaml-csharp", "yamldotnet")
        self.assertGreater(band, R.MEMORY_BAND)
        self.assertEqual(R.verdict(1090, 1000, band), "tie")
        self.assertEqual(R.verdict(1130, 1000, band), "loss")
        self.assertEqual(R.verdict(870, 1000, band), "win")
        # Every other pair keeps the narrow band.
        for pair in (("oneyaml-csharp", "vyaml"), ("ours", "rival"), ("oneyaml-go", "go-yaml-v3")):
            self.assertEqual(R.memory_band(*pair), R.MEMORY_BAND)

    def test_row_verdict_uses_the_band_of_the_pair(self):
        def row(rival):
            cells = [cell("ours", "own", peak=1090, language="csharp"), cell(rival, peak=1000, language="csharp")]
            return R.compare_rows(cells)[0]["own"][0]["memory"]["verdict"]
        self.assertEqual(row("yamldotnet"), "tie")
        self.assertEqual(row("vyaml"), "loss")


class Rows(unittest.TestCase):
    def test_reference_is_the_best_competitor_not_the_first_or_ours(self):
        cells = [cell("ours", "own", t=120), cell("slow", t=300), cell("fast", t=100), cell("mid", t=110)]
        [row] = R.compare_rows(cells)
        self.assertEqual(row["best"]["time"]["library"], "fast")
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
        self.assertEqual(row["best"]["time"]["library"], "real")
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
        self.assertEqual(row["best"]["time"]["library"], "rival")


class WhatMaySetTheReference(unittest.TestCase):
    """A cell that did not do the full task as specified is never called the best, but a row is always
    compared with the strongest competitor in it, and a loss to a marked one is a loss."""

    def test_a_faster_marked_cell_is_the_one_ours_is_compared_with_and_it_is_named_with_its_mark(self):
        for variant in ("bytes-only", "value-only", "harness-rendered", "something-new"):
            fast = cell("loose", t=10, variant=variant)
            [row] = R.compare_rows([cell("ours", "own", t=100), fast, cell("real", t=200)])
            self.assertEqual(row["best"]["time"]["library"], "loose", variant)
            self.assertEqual(row["own"][0]["time"]["vs_mark"], variant)
            e = row["own"][0]["time"]
            self.assertEqual((e["vs"], e["vs_mark"], e["verdict"]), ("loose", variant, "loss"), variant)
            self.assertEqual(R.non_reference_label(fast), variant)
        text = cell("patcher", t=10)
        text["state"] = "text"
        [row] = R.compare_rows([cell("ours", "own", t=100), text, cell("real", t=200)])
        self.assertEqual(row["own"][0]["time"]["verdict"], "loss")
        self.assertEqual(row["own"][0]["time"]["vs_mark"], "text API")
        self.assertEqual(R.tally([row], "time")["loss"], 1)
        both = cell("patcher", t=10, variant="value-only")
        both["state"] = "text"
        self.assertEqual(R.non_reference_label(both), "text API, value-only")

    def test_a_marked_cell_that_is_slower_does_not_change_the_comparison(self):
        slow = cell("loose", t=1000, variant="bytes-only")
        [row] = R.compare_rows([cell("ours", "own", t=100), slow, cell("real", t=200)])
        self.assertEqual(row["best"]["time"]["library"], "real")
        self.assertEqual(row["own"][0]["time"]["vs_mark"], "")
        self.assertEqual(row["own"][0]["time"]["verdict"], "win")

    def test_a_tie_goes_to_the_unmarked_cell(self):
        [row] = R.compare_rows([cell("ours", "own", t=100), cell("loose", t=50, variant="bytes-only"), cell("real", t=50)])
        self.assertEqual(row["best"]["time"]["library"], "real")

    def test_memory_is_compared_with_a_marked_cell_too(self):
        [row] = R.compare_rows([cell("ours", "own", t=100, peak=1000), cell("loose", t=500, peak=100, variant="value-only"),
                                cell("real", t=200, peak=500)])
        m = row["own"][0]["memory"]
        self.assertEqual((m["vs"], m["vs_mark"], m["verdict"]), ("loose", "value-only", "loss"))
        self.assertEqual(row["own"][0]["time"]["verdict"], "win")

    def test_a_merged_cell_may_set_the_reference(self):
        [row] = R.compare_rows([cell("ours", "own", t=100), cell("merger", t=10, variant="merged"), cell("real", t=200)])
        self.assertEqual(row["best"]["time"]["library"], "merger")
        self.assertEqual(R.non_reference_label(cell("merger", variant="merged")), "")

    def test_a_row_of_only_marked_cells_is_compared_with_the_strongest_of_them_and_a_loss_is_a_loss(self):
        # The only lossless competitor of a task is often a bytes-only one. Ours losing to it is a loss.
        [row] = R.compare_rows([cell("ours", "own", t=100, peak=1000), cell("loose", t=10, peak=100, variant="bytes-only")])
        self.assertEqual(row["best"]["time"]["library"], "loose")
        e = row["own"][0]
        self.assertEqual((e["time"]["verdict"], e["time"]["vs_mark"]), ("loss", "bytes-only"))
        self.assertEqual(e["memory"]["verdict"], "loss")
        self.assertEqual(R.tally([row], "time")["loss"], 1)
        self.assertEqual(R.tally([row], "time")["no-rival"], 0)
        self.assertEqual(R.tally([row], "memory")["loss"], 1)

    def test_a_row_is_no_rival_only_when_no_competitor_answered(self):
        [row] = R.compare_rows([cell("ours", "own", t=100), cell("bad", status="differs", detail="x"),
                                cell("loose", status="unsupported")])
        self.assertIsNone(row["best"]["time"])
        self.assertIsNone(row["own"][0]["time"])
        self.assertEqual(R.tally([row], "time")["no-rival"], 1)

    def test_the_best_in_a_row_is_a_full_task_cell_when_there_is_one(self):
        c = [cell("a", t=100), cell("loose", t=10, variant="bytes-only"), cell("b", t=300)]
        [row] = R.compare_rows(c)
        self.assertEqual(R.best_in_row(row, "time")["library"], "a")
        [row] = R.compare_rows([cell("loose", t=10, variant="bytes-only"), cell("l2", t=20, variant="value-only")])
        self.assertEqual(R.best_in_row(row, "time")["library"], "loose")


def result(cells, mode="quick", smoke=True):
    return {"format": R.FORMAT, "harnesses": {}, "workloads": [],
            "run": {"date": "d", "mode": mode, "smoke": smoke, "environment": {}, "load": {}},
            "cells": cells}


class Validation(unittest.TestCase):
    def test_a_good_result_passes(self):
        R.validate(result([cell("a", "own"), cell("b", status="differs"), cell("c", status="unsupported")]))

    def test_a_wrong_answer_may_not_carry_a_number(self):
        for status in ("differs", "rejects", "unjudged", "error"):
            bad = cell("a", "own", status=status)
            bad["time"] = {"ns": 1.0}
            with self.assertRaisesRegex(R.ResultError, "carries numbers"):
                R.validate(result([bad]))

    def test_an_ok_cell_needs_both_figures(self):
        bad = cell("a", "own")
        del bad["memory"]
        with self.assertRaisesRegex(R.ResultError, "lacks memory"):
            R.validate(result([bad]))
        bad = cell("a", "own")
        bad["time"]["ns"] = 0
        with self.assertRaisesRegex(R.ResultError, "positive time"):
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

    def test_a_quick_run_is_a_smoke_run(self):
        with self.assertRaisesRegex(R.ResultError, "smoke"):
            R.validate(result([cell("a")], mode="quick", smoke=False))
        R.validate(result([cell("a")], mode="full", smoke=False))

    def test_wrong_format_is_refused(self):
        r = result([])
        r["format"] = "other/1"
        with self.assertRaises(R.ResultError):
            R.validate(r)
