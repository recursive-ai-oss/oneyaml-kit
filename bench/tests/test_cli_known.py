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


class OptionalLibraries(unittest.TestCase):
    """Entries for a library that is compiled in only when a variable names its checkout."""

    ENTRY = {"cell": "extra/dump/w", "status": "differs", "reason": "x"}

    def run_check(self, env, in_build, cell_status="differs", entries=None):
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "known.json").write_text(json.dumps({"entries": [self.ENTRY] if entries is None else entries}), encoding="utf-8")
            harness = {"language": "go", "_dir": d, "optional_libraries": {"KIT_EXTRA_SRC": ["extra"]}}
            libs = [{"id": "other", "role": "competitor"}] + (
                [{"id": "extra", "role": "own"}] if in_build else [])
            cells = [cell("other", "ok")] + ([cell("extra", cell_status)] if in_build else [])
            for c in cells:
                c["role"] = "own" if c["library"] == "extra" else "competitor"
            result = {"cells": cells, "harnesses": {"go": {"libraries": libs}}}
            with mock.patch.object(cli.run, "discover", return_value={"go": harness}), \
                    mock.patch.object(cli.workloads, "universe", return_value={"w": ["dump"]}):
                return cli.known_problems(result, env), cli.known_unchecked(result, env)

    def test_an_absent_library_is_not_stale(self):
        problems, unchecked = self.run_check({}, in_build=False)
        self.assertEqual(problems, [])
        self.assertEqual(unchecked, {"extra (KIT_EXTRA_SRC is not set)": ["extra/dump/w"]})

    def test_without_the_variable_the_entries_would_be_stale(self):
        # What the absent case guards against: the same run, with the variable counted as set.
        problems, _ = self.run_check({"KIT_EXTRA_SRC": "/x"}, in_build=False)
        self.assertEqual(len(problems), 1)
        self.assertIn("stale known deviation extra/dump/w", problems[0])

    def test_a_present_library_is_checked_and_matches(self):
        problems, unchecked = self.run_check({"KIT_EXTRA_SRC": "/x"}, in_build=True)
        self.assertEqual(problems, [])
        self.assertEqual(unchecked, {})

    def test_a_present_library_with_a_stale_entry_fails(self):
        problems, _ = self.run_check({"KIT_EXTRA_SRC": "/x"}, in_build=True, cell_status="ok")
        self.assertEqual(len(problems), 1)
        self.assertIn("stale known deviation extra/dump/w: it is now ok", problems[0])

    def test_a_present_library_with_an_unlisted_deviation_fails(self):
        problems, _ = self.run_check({"KIT_EXTRA_SRC": "/x"}, in_build=True, entries=[])
        self.assertEqual(len(problems), 1)
        self.assertIn("unlisted deviation extra/dump/w", problems[0])

    def test_the_harnesses_name_their_optional_libraries(self):
        for lang, var in (("go", "KIT_ONEYAML_GO_SRC"), ("csharp", "KIT_ONEYAML_CSHARP_SRC")):
            h = cli.run.discover()[lang]
            self.assertIn(var, h["optional_libraries"], lang)


if __name__ == "__main__":
    unittest.main()
