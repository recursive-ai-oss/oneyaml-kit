"""The summary `bin/bench run` prints for the lists of known deviations."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneyaml_bench import cli  # noqa: E402


def cell(lib, status, task="dump", workload="w", variant=None):
    c = {"library": lib, "task": task, "workload": workload, "language": "go", "role": "competitor",
         "status": status}
    if variant:
        c["variant"] = variant
    return c


KNOWN = {"entries": [
    {"cell": "a/dump/w", "status": "differs", "reason": "x"},
    {"cell": "b/dump/w", "status": "differs", "reason": "x"},
    {"cell": "gone/dump/w", "status": "differs", "reason": "x"},
]}


class KnownCounts(unittest.TestCase):
    def counts(self, cells, problems):
        with mock.patch.object(cli, "known_lists", return_value={"go": KNOWN}):
            return cli.known_counts({"cells": cells}, problems)

    def test_a_matching_list_is_all_listed(self):
        cells = [cell("a", "differs"), cell("b", "differs"), cell("c", "ok")]
        self.assertEqual(self.counts(cells, []), {"listed": 2, "unlisted": 0, "stale": 0, "other": 0})

    def test_each_kind_of_problem_is_counted_on_its_own(self):
        cells = [cell("a", "differs"), cell("b", "ok"), cell("d", "rejects")]
        problems = ["go: unlisted deviation d/dump/w: rejects",
                    "go: stale known deviation b/dump/w: it is now ok",
                    "go: stale known deviation gone/dump/w: no library 'gone'",
                    "go: a/dump/w is listed as differs but is rejects"]
        self.assertEqual(self.counts(cells, problems), {"listed": 1, "unlisted": 1, "stale": 2, "other": 1})


if __name__ == "__main__":
    unittest.main()
