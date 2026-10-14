"""Self-tests for the report generator: what it shows, what it must not show, and that the
charts are well formed and carry their numbers."""

import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from oneyaml_bench import report as P  # noqa: E402
from oneyaml_bench import results as R  # noqa: E402
from oneyaml_kit import public_text  # noqa: E402
from test_results import cell  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "report_golden.md"


def synthetic() -> dict:
    cells = []
    # w1: we win time and lose memory; w2: we lose time; w3: a rival is wrong; w4: we are wrong.
    for w, ours, rival, other in (("w1", (50.0, 900), (100.0, 400), (120.0, 800)),
                                  ("w2", (200.0, 100), (100.0, 100), (150.0, 100))):
        cells += [cell("ours", "own", workload=w, t=ours[0], peak=ours[1]),
                  cell("rival", workload=w, t=rival[0], peak=rival[1]),
                  cell("other", workload=w, t=other[0], peak=other[1])]
    cells += [cell("ours", "own", workload="w3", t=80.0), cell("rival", workload="w3", t=100.0),
              cell("other", workload="w3", status="differs", detail="at /a/b: got 1, expected 2")]
    cells += [cell("ours", "own", workload="w4", status="differs", detail="byte 3: got x, expected y"),
              cell("rival", workload="w4", t=100.0), cell("other", workload="w4", status="unsupported")]
    cells += [cell("ours", "own", workload="w1", task="dump", t=90.0),
              cell("rival", workload="w1", task="dump", status="unjudged", detail="the two reference parsers disagree at /a"),
              cell("other", workload="w1", task="dump", t=100.0)]
    cells += [cell("ours", "own", workload="w1", task="patch", status="unsupported"),
              cell("rival", workload="w1", task="patch", status="rejects", detail="boom | pipe")]
    wl = [{"id": f"w{i}", "kind": "generated", "tier": "quick", "description": "d", "documents": 1,
           "input_bytes": 1000 * i, "tasks": ["generic-load"], "source": {"origin": "generated", "generator": "g", "licence": "CC0-1.0"}}
          for i in (1, 2, 3, 4)]
    return {
        "format": R.FORMAT,
        "run": {"date": "2026-01-01T00:00:00Z", "mode": "quick", "seconds": 1.5, "restarts": 0, "counters": False,
                "environment": {"cpu": "Test CPU", "cpus": 4, "memory_gb": 8.0, "os": "Linux", "machine": "x86_64",
                                "governor": "performance", "boost": "disabled", "pinned_cpu": None, "kit_revision": "abc"},
                "publishability": R.assess_publishability("quick", [0.1] * 10, 0.5, 0.5, True),
                "load": {"before": 0.5, "after": 0.5}},
        "harnesses": {"rust": {"name": "h", "version": "1", "runtime": "rustc x", "target": "t", "flags": {"opt": 3},
                               "config": {"warmup_ms": 1, "samples": 5, "sample_target_ms": 1}, "timer": "tm", "memory": "mm",
                               "libraries": [{"id": "ours", "package": "ours", "version": "1.0", "role": "own", "settings": {}},
                                             {"id": "rival", "package": "rival", "version": "2.0", "role": "competitor", "settings": {}},
                                             {"id": "other", "package": "other", "version": "3.0", "role": "competitor", "settings": {"k": "v"}}]}},
        "workloads": wl,
        "cells": cells,
    }


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        cls.md = P.render(synthetic(), cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_matches_the_golden_report(self):
        if os.environ.get("UPDATE_GOLDEN"):
            GOLDEN.write_text(self.md, encoding="utf-8")
        self.assertEqual(self.md, GOLDEN.read_text(encoding="utf-8"),
                         "the report changed; if that is intended, rerun with UPDATE_GOLDEN=1")

    def test_a_smoke_run_is_labelled_so(self):
        self.assertIn("SMOKE RESULT, NOT PUBLISHABLE", self.md)
        self.assertIn("the run was a quick run", self.md)

    def test_losses_are_marked_where_they_happen(self):
        self.assertIn("2.00× of `rival` ▼ loss", self.md)  # w2 time
        self.assertIn("2.25× of `rival` ▼ loss", self.md)  # w1 memory
        self.assertIn("0.50× of `rival` ▲ win", self.md)   # w1 time

    def test_a_wrong_or_missing_answer_shows_no_figure(self):
        row = next(l for l in self.md.splitlines() if l.startswith("| `w4`"))
        self.assertIn("differs", row)
        self.assertIn("no number (differs)", row)
        self.assertNotRegex(row.split("|")[3], r"\d")  # our column holds no figure
        w3 = next(l for l in self.md.splitlines() if l.startswith("| `w3`") and "differs" in l)
        self.assertIn("differs", w3)

    def test_the_reference_is_the_best_competitor(self):
        # In w1 the faster competitor is `rival` (100) not `other` (120).
        self.assertIn("0.50× of `rival`", self.md)
        # In w3 `other` is wrong, so `rival` stays the reference.
        self.assertIn("0.80× of `rival`", self.md)

    def test_cells_without_numbers_are_listed_with_reasons_and_pipes_escaped(self):
        self.assertIn("## Cells without a number", self.md)
        self.assertIn("at /a/b: got 1, expected 2", self.md)
        self.assertIn("boom \\| pipe", self.md)

    def test_alt_text_carries_the_numbers(self):
        alts = [l for l in self.md.splitlines() if "<img alt=" in l]
        self.assertTrue(alts)
        time_alt = next(a for a in alts if "generic-load (rust): time" in a)
        for needle in ("w1 0.50×", "w2 2.00×", "w3 0.80×", "loss", "ours 200 ns vs rival 100 ns",
                       "Ours ÷ best competitor"):
            self.assertIn(needle, time_alt)

    def test_charts_are_well_formed_light_and_dark(self):
        files = sorted((self.out / "charts").glob("*.svg"))
        self.assertTrue(files)
        names = {f.name for f in files}
        self.assertIn("rust-generic-load-time-light.svg", names)
        self.assertIn("rust-generic-load-time-dark.svg", names)
        for f in files:
            root = ET.fromstring(f.read_text(encoding="utf-8"))
            self.assertTrue(root.tag.endswith("svg"))
            self.assertTrue(root.get("aria-label"))
            self.assertTrue(any(c.tag.endswith("title") for c in root))
        light = (self.out / "charts" / "rust-generic-load-time-light.svg").read_text(encoding="utf-8")
        dark = (self.out / "charts" / "rust-generic-load-time-dark.svg").read_text(encoding="utf-8")
        self.assertIn("#ffffff", light)
        self.assertIn("#0d1117", dark)
        self.assertNotEqual(light, dark)

    def test_a_loss_bar_is_in_the_loss_colour_and_a_win_and_a_loss_are_equally_long(self):
        # Rows are w1 (0.5x, a win), w2 (2x, a loss), w3 (0.8x, a win). The colours are written
        # out on purpose: a chart that painted a loss like a win would defeat showing losses.
        # Twice as fast and twice as slow are the same length on the log axis, on opposite sides
        # of the 1.0x line, so a loss is not squeezed.
        for theme, win, loss in (("light", "#1a7f37", "#cf222e"), ("dark", "#3fb950", "#ff7b72")):
            text = (self.out / "charts" / f"rust-generic-load-time-{theme}.svg").read_text(encoding="utf-8")
            root = ET.fromstring(text)
            bars = [r for r in root.iter("{http://www.w3.org/2000/svg}rect") if r.get("height") == "16"]
            self.assertEqual([r.get("fill") for r in bars], [win, loss, win], theme)
            centre = 450.0
            w1, w2 = bars[0], bars[1]
            self.assertAlmostEqual(float(w1.get("x")) + float(w1.get("width")), centre, places=0)
            self.assertAlmostEqual(float(w2.get("x")), centre, places=0)
            self.assertAlmostEqual(float(w1.get("width")), float(w2.get("width")), places=1)
            self.assertIn("2.00×  ▼ loss", text)

    def test_every_chart_and_table_uses_the_same_ratio(self):
        # The ratio is ours / best competitor everywhere: 0.50x for the win, 2.00x for the loss.
        self.assertIn("0.50× of `rival` ▲ win", self.md)
        self.assertIn("2.00× of `rival` ▼ loss", self.md)
        alt = next(l for l in self.md.splitlines() if "generic-load (rust): time" in l)
        self.assertIn("w1 0.50×", alt)
        self.assertIn("w2 2.00×", alt)
        self.assertNotIn("faster", alt.split("workloads:")[0] + "")

    def test_an_unjudged_cell_is_shown_as_such_and_not_as_a_wrong_answer(self):
        row = next(l for l in self.md.splitlines() if l.startswith("| `w1`") and "unjudged" in l)
        self.assertIn("unjudged", row)
        self.assertIn("parsers disagree", self.md)
        self.assertIn("not a verdict on the library", self.md)

    def test_the_report_passes_the_public_text_check(self):
        for line in self.md.splitlines():
            self.assertIsNone(public_text.PATTERN.search(line), line)

    def test_an_invalid_result_produces_no_report(self):
        bad = synthetic()
        bad["cells"][0]["status"] = "differs"
        with tempfile.TemporaryDirectory() as t, self.assertRaises(R.ResultError):
            P.render(bad, Path(t))

    def test_formatting(self):
        self.assertEqual(P.fmt_time(950), "950 ns")
        self.assertEqual(P.fmt_time(1500), "1.5 µs")
        self.assertEqual(P.fmt_time(2_500_000), "2.5 ms")
        self.assertEqual(P.fmt_bytes(1536), "1.5 KiB")
        self.assertEqual(P.fmt_ratio(1.234), "1.23×")
        self.assertEqual(P.fmt_ratio(12.34), "12.3×")


def baseline_result() -> dict:
    """A language with competitors only: three libraries on two workloads, one of them wrong in the second."""
    cells = []
    for w, (a, b, c) in (("w1", ((100.0, 1000), (200.0, 4000), (400.0, 2000))),
                         ("w2", ((100.0, 1000), (100.0, 1000), (200.0, 500)))):
        cells += [cell("alpha", workload=w, language="csharp", t=a[0], peak=a[1]),
                  cell("beta", workload=w, language="csharp", t=b[0], peak=b[1]),
                  cell("gamma", workload=w, language="csharp", t=c[0], peak=c[1])]
    cells.append(cell("delta", workload="w1", language="csharp", status="differs", detail="at /a: got 1, expected 2"))
    res = synthetic()
    res["cells"] = cells
    res["workloads"] = [w for w in res["workloads"] if w["id"] in ("w1", "w2")]
    res["harnesses"] = {"csharp": {**res["harnesses"]["rust"], "libraries": [
        {"id": n, "package": n, "version": "1", "role": "competitor", "settings": {}}
        for n in ("alpha", "beta", "gamma", "delta")]}}
    return res


class BaselineReportTests(unittest.TestCase):
    """A language that has no library of ours is a table of competitors and a baseline to beat."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        cls.md = P.render(baseline_result(), cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_tables_name_the_best_library_and_make_no_claim_about_ours(self):
        self.assertIn("Best in the row", self.md)
        self.assertNotIn("Ours vs best competitor", self.md)
        row = next(l for l in self.md.splitlines() if l.startswith("| `w1`") and "alpha" in l)
        self.assertTrue(row.rstrip().endswith("| `alpha` |"), row)
        mem = [l for l in self.md.splitlines() if l.startswith("| `w1`") and "KiB" in l or "B /" in l]
        self.assertTrue(any(l.rstrip().endswith("| `alpha` |") for l in mem))

    def test_the_summary_is_a_geometric_mean_against_the_best_in_each_row(self):
        self.assertIn("Languages without a library of ours", self.md)
        rows = [l for l in self.md.splitlines() if l.startswith("| csharp | generic-load |")]
        by = {r.split("|")[3].strip(" `"): [c.strip() for c in r.split("|")[4:9]] for r in rows}
        # beta: sqrt(2.00 * 1.00) = 1.41; gamma: sqrt(4.00 * 2.00) = 2.83; alpha is the best in both rows.
        self.assertEqual(by["alpha"][:4], ["2", "2", "1.00×", "1"])
        self.assertEqual(by["beta"][2], "1.41×")
        self.assertEqual(by["gamma"][2], "2.83×")
        # A library with no number in a row is judged on the rows where it has one.
        self.assertNotIn("delta", by)
        # Peak: w1 alpha 1000 best, w2 gamma 500 best. gamma: sqrt(2 * 1) = 1.41.
        self.assertEqual(by["gamma"][3:5], ["1", "1.41×"])

    def test_the_charts_exist_and_their_alt_text_carries_the_numbers(self):
        names = {f.name for f in (self.out / "charts").glob("*.svg")}
        self.assertIn("csharp-generic-load-time-light.svg", names)
        self.assertIn("csharp-generic-load-memory-dark.svg", names)
        alt = next(l for l in self.md.splitlines() if "generic-load (csharp): time" in l and "<img alt=" in l)
        for needle in ("beta 1.41× over 2 rows, best in 0", "gamma 2.83× over 2 rows", "alpha 1.00× over 2 rows, best in 2"):
            self.assertIn(needle, alt)
        text = (self.out / "charts" / "csharp-generic-load-time-light.svg").read_text(encoding="utf-8")
        root = ET.fromstring(text)
        self.assertTrue(root.get("aria-label"))
        self.assertIn("2 rows, best in 2", text)
        # The bars are neutral: there is no win or loss between competitors.
        self.assertNotIn("#cf222e", text)
        self.assertNotIn("#1a7f37", text)

    def test_a_wrong_answer_is_still_listed_and_the_report_is_clean(self):
        self.assertIn("at /a: got 1, expected 2", self.md)
        for line in self.md.splitlines():
            self.assertIsNone(public_text.PATTERN.search(line), line)
        # A report that has our library in it keeps the old summary, and one without it does not print an empty one.
        self.assertNotIn("Weakest row", self.md)


if __name__ == "__main__":
    unittest.main()
