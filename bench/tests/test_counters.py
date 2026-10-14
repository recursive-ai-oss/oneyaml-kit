"""Self-tests for the deterministic counters gate: what counts as a regression."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneyaml_bench import cli  # noqa: E402
from test_results import cell  # noqa: E402


def result(*cells):
    return {"cells": list(cells)}


def view(allocs, allocated=1000, peak=1000, instructions=None):
    c = cell("ours", "own", workload="w")
    c["memory"] = {"peak_bytes": peak, "allocated_bytes": allocated, "allocations": allocs}
    if instructions:
        c["instructions"] = instructions
    return cli.counter_view(result(c))


class Counters(unittest.TestCase):
    def test_only_our_own_correct_cells_are_baselined(self):
        v = cli.counter_view(result(cell("ours", "own"), cell("rival"), cell("bad", "own", status="differs")))
        self.assertEqual(list(v), ["rust/ours/generic-load/w"])

    def test_growth_beyond_the_tolerance_fails_and_shrinking_does_not(self):
        base = view(1000)
        self.assertEqual(cli.compare_counters(view(1000), base), [])
        self.assertEqual(cli.compare_counters(view(900), base), [])
        self.assertEqual(cli.compare_counters(view(1020), base), [])  # inside the 3% allowance
        [p] = cli.compare_counters(view(1200), base)
        self.assertIn("allocations rose from 1000 to 1200", p)

    def test_each_figure_has_its_own_tolerance(self):
        base = view(1000, allocated=10_000, peak=10_000, instructions=1_000_000)
        self.assertEqual(cli.compare_counters(view(1000, 10_050, 10_050, 1_010_000), base), [])
        self.assertEqual(len(cli.compare_counters(view(1000, 10_500, 10_000, 1_000_000), base)), 1)
        self.assertEqual(len(cli.compare_counters(view(1000, 10_000, 10_500, 1_000_000), base)), 1)
        self.assertEqual(len(cli.compare_counters(view(1000, 10_000, 10_000, 1_050_000), base)), 1)

    def test_a_baseline_entry_that_was_not_measured_is_stale(self):
        base = view(1000)
        [p] = cli.compare_counters({}, base)
        self.assertIn("stale baseline entry", p)

    def test_a_new_cell_is_not_a_failure(self):
        self.assertEqual(cli.compare_counters(view(5), {}), [])


if __name__ == "__main__":
    unittest.main()
