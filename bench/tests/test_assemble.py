"""Self-tests for how the driver turns harness output into a result."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneyaml_bench import results, run  # noqa: E402


def harness():
    return {"name": "h", "version": "1", "libraries": [{"id": "ours", "role": "own"}, {"id": "rival", "role": "competitor"}]}


def ok(lib, wl):
    return {"type": "cell", "library": lib, "task": "generic-load", "workload": wl, "status": "ok", "input_bytes": 1,
            "time": {"median_ns": 1.0, "mad_ns": 0.0, "samples": 31},
            "memory": {"method": "m", "peak_bytes": 1, "allocated_bytes": 1, "allocations": 1}}


def bad(lib, wl):
    return {"type": "cell", "library": lib, "task": "generic-load", "workload": wl, "status": "error",
            "detail": "the process died", "input_bytes": None}


def assemble(cells, expected):
    metas = [{"id": "w", "kind": "generated", "tier": "quick", "description": "d", "documents": 1,
              "input_bytes": 1, "tasks": ["generic-load"], "source": {}}]
    return run.assemble("full", {"rust": harness()}, {"rust": cells}, metas, [0.1] * 10, 0.5, 0.5, 1.0, 0, expected, 2, False)


class Complete(unittest.TestCase):
    def reasons(self, res):
        return res["run"]["publishability"]["reasons"]

    def test_every_cell_measured_is_complete(self):
        res = assemble([ok("ours", "w"), ok("rival", "w")], 2)
        self.assertFalse([r for r in self.reasons(res) if "finish" in r])
        results.validate(res)

    def test_a_missing_cell_is_incomplete(self):
        res = assemble([ok("ours", "w")], 2)
        self.assertTrue([r for r in self.reasons(res) if "did not finish" in r])

    def test_a_crashed_cell_is_incomplete_even_though_it_has_a_record(self):
        res = assemble([ok("ours", "w"), bad("rival", "w")], 2)
        self.assertTrue([r for r in self.reasons(res) if "did not finish" in r])
        self.assertFalse(res["run"]["publishability"]["publishable"])

    def test_pinning_is_recorded_and_judged(self):
        res = assemble([ok("ours", "w"), ok("rival", "w")], 2)
        self.assertEqual(res["run"]["environment"]["pinned_cpu"], 2)
        self.assertEqual(res["run"]["publishability"]["measured"]["pinned_cpu"], 2)
        self.assertFalse([r for r in self.reasons(res) if "pinned" in r])


class TextApi(unittest.TestCase):
    def test_a_library_that_takes_and_returns_text_is_flagged_so_it_never_sets_the_best(self):
        h = harness()
        h["libraries"].append({"id": "patcher", "role": "competitor", "settings": {"state": "the text"}})
        metas = [{"id": "w", "kind": "generated", "tier": "quick", "description": "d", "documents": 1,
                  "input_bytes": 1, "tasks": ["generic-load"], "source": {}}]
        res = run.assemble("quick", {"go": h}, {"go": [ok("ours", "w"), ok("rival", "w"), ok("patcher", "w")]},
                           metas, [0.1] * 10, 0.5, 0.5, 1.0, 0, 3, None, False)
        by = {c["library"]: c for c in res["cells"]}
        self.assertEqual(by["patcher"]["state"], "text")
        self.assertNotIn("state", by["rival"])
        [row] = results.compare_rows(res["cells"])
        self.assertEqual(row["best_time"]["library"], "rival")


if __name__ == "__main__":
    unittest.main()
