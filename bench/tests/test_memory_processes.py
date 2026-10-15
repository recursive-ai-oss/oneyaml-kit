"""The driver's extra memory processes: a harness whose memory figure differs between processes declares how many
measure it, and the driver merges their figures into the cells of the main run. Times are never touched."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneyaml_bench import results as R, run  # noqa: E402


def ok(lib, peak, alloc=None, workload="w"):
    return {"type": "cell", "library": lib, "task": "generic-load", "workload": workload, "status": "ok",
            "time": {"median_ns": 5.0, "mad_ns": 0.1, "samples": 30},
            "memory": {"method": "m", "peak_bytes": peak, "allocated_bytes": alloc or peak, "allocations": None}}


def bad(lib, workload="w"):
    return {"type": "cell", "library": lib, "task": "generic-load", "workload": workload, "status": "differs",
            "detail": "x"}


class ProcessCount(unittest.TestCase):
    def test_the_harness_declares_it_per_mode_and_the_command_line_overrides(self):
        h = {"memory_processes": {"full": 5}}
        self.assertEqual(run.memory_process_count(h, "full", None), 5)
        self.assertEqual(run.memory_process_count(h, "quick", None), 1)  # quick mode stays at one process
        self.assertEqual(run.memory_process_count({}, "full", None), 1)  # a harness that declares nothing
        self.assertEqual(run.memory_process_count(h, "full", 3), 3)
        self.assertEqual(run.memory_process_count(h, "quick", 2), 2)
        self.assertEqual(run.memory_process_count(h, "full", 0), 1)

    def test_the_csharp_harness_declares_five_processes_in_full_mode_only(self):
        h = run.discover()["csharp"]
        self.assertEqual(h["memory_processes"], {"full": 5})
        self.assertNotIn("memory_processes", run.discover()["rust"])


class Merge(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.real = run.run_harness
        self.addCleanup(setattr, run, "run_harness", self.real)

    def fake(self, values):
        """A harness that answers each memory-mode run with the next list of peaks, for cells a, b and c."""
        it = iter(values)

        def run_harness(h, build_dir, wl_dir, mode, out_dir, sampler, pin_cpu, libraries, tasks, only, log,
                        skip_cells=()):
            self.calls.append((mode, list(skip_cells)))
            peaks = next(it)
            cells = [ok(lib, p) for lib, p in zip("abc", peaks) if p is not None]
            return {}, cells, 0
        run.run_harness = run_harness

    def test_each_ok_cell_gets_the_median_of_all_processes_and_failed_cells_are_not_rerun(self):
        cells = [ok("a", 1000), bad("b"), ok("c", 50)]
        times = copy.deepcopy([c.get("time") for c in cells])
        self.fake([[1100, None, 50], [900, None, 60], [1000, None, 50]])
        run.add_memory_processes({}, Path("b"), Path("w"), Path("o"), None, None, [], [], [], cells, 4, lambda *_: None)
        self.assertEqual([m for m, _ in self.calls], ["memory"] * 3)
        self.assertEqual(self.calls[0][1], ["b/generic-load/w"])  # the cell that failed is skipped
        a, b, c = cells
        self.assertEqual(a["memory"]["peak_bytes"], 1000)
        self.assertEqual(a["memory"]["processes"]["peak_bytes"], [1000, 1100, 900, 1000])
        self.assertEqual(c["memory"]["peak_bytes"], 50)
        self.assertNotIn("memory", b)
        self.assertEqual([c.get("time") for c in cells], times)  # times stay as the main run measured them
        full = {"language": "csharp", "role": "competitor", "input_bytes": 1, "variant": "spec", **a}
        R.check_memory_processes(("csharp", "a", "generic-load", "w"), full["memory"])

    def test_a_process_that_lost_a_cell_leaves_the_others_to_count(self):
        cells = [ok("a", 1000), ok("b", 2000)]
        self.fake([[1200, None, None], [800, 2100, None]])
        run.add_memory_processes({}, Path("b"), Path("w"), Path("o"), None, None, [], [], [], cells, 3, lambda *_: None)
        self.assertEqual(cells[0]["memory"]["processes"]["count"], 3)
        self.assertEqual(cells[1]["memory"]["processes"]["count"], 2)
        self.assertEqual(cells[1]["memory"]["peak_bytes"], 2050)


if __name__ == "__main__":
    unittest.main()
