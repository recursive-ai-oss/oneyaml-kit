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
        for needle in ("w1 2.00×", "w2 0.50×", "w3 1.25×", "loss", "ours 200 ns vs rival 100 ns"):
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

    def test_a_loss_bar_is_in_the_loss_colour_and_labelled(self):
        # Rows are w1 (win), w2 (loss), w3 (win). The colours are written out here on purpose:
        # a chart that painted a loss like a win would defeat the point of showing losses.
        for theme, win, loss in (("light", "#1a7f37", "#cf222e"), ("dark", "#3fb950", "#ff7b72")):
            text = (self.out / "charts" / f"rust-generic-load-time-{theme}.svg").read_text(encoding="utf-8")
            root = ET.fromstring(text)
            bars = [r.get("fill") for r in root.iter("{http://www.w3.org/2000/svg}rect") if r.get("x") == "230"]
            self.assertEqual(bars, [win, loss, win], theme)
            self.assertIn("0.50×  ▼ loss", text)

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


if __name__ == "__main__":
    unittest.main()
