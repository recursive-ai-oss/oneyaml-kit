"""Self-tests for how the driver turns harness output into a result."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneyaml_bench import results, run  # noqa: E402


def harness(extra=()):
    return {"name": "h", "version": "1", "libraries": [{"id": "ours", "role": "own"}, {"id": "rival", "role": "competitor"},
                                                     *extra]}


def ok(lib, wl="w", ns=1.0):
    return {"type": "cell", "library": lib, "task": "generic-load", "workload": wl, "status": "ok", "input_bytes": 1,
            "time": {"ns": ns, "stat": "mean"}, "memory": {"allocated_bytes": 1, "allocations": 1}}


METAS = [{"id": "w", "kind": "generated", "tier": "quick", "description": "d", "documents": 1,
          "input_bytes": 1, "tasks": ["generic-load"], "source": {}}]


def assemble(cells, h=None, mode="quick", smoke=True):
    return run.assemble(mode, smoke, {"go": h or harness()}, {"go": cells}, METAS, 0.5, 0.5, 1.0)


class Assemble(unittest.TestCase):
    def test_roles_come_from_the_harness_record_and_the_result_validates(self):
        res = assemble([ok("ours"), ok("rival")])
        self.assertEqual({c["library"]: c["role"] for c in res["cells"]}, {"ours": "own", "rival": "competitor"})
        results.validate(res)

    def test_a_library_that_takes_and_returns_text_is_flagged_so_it_never_sets_the_best(self):
        h = harness([{"id": "patcher", "role": "competitor", "settings": {"state": "the text"}}])
        res = assemble([ok("ours"), ok("rival", ns=5.0), ok("patcher", ns=1.0)], h)
        by = {c["library"]: c for c in res["cells"]}
        self.assertEqual(by["patcher"]["state"], "text")
        self.assertNotIn("state", by["rival"])
        [row] = results.compare_rows(res["cells"])
        # Faster, so it is the one compared with; the mark says it did less.
        self.assertEqual(row["own"][0]["time"]["vs"], "patcher")
        self.assertEqual(row["own"][0]["time"]["vs_mark"], "text API")

    def test_a_quick_run_is_a_smoke_run_whatever_is_asked(self):
        self.assertTrue(assemble([ok("ours")])["run"]["smoke"])

    def test_a_crashed_cell_keeps_its_record_and_no_numbers(self):
        bad = {"type": "cell", "library": "rival", "task": "generic-load", "workload": "w", "status": "error",
               "detail": "the process died", "input_bytes": None}
        results.validate(assemble([ok("ours"), bad]))


class ParseLines(unittest.TestCase):
    def test_a_line_cut_short_by_a_crash_is_ignored_and_the_cell_in_flight_is_named(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.jsonl"
            p.write_text('{"type": "harness", "name": "h"}\n{"type": "begin", "cell": "a/b/c"}\n{"type": "ce', encoding="utf-8")
            h, cells, begun = run.parse_lines(p)
        self.assertEqual((h["name"], cells, begun), ("h", [], ["a/b/c"]))


if __name__ == "__main__":
    unittest.main()
