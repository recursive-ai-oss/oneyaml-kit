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

    def test_a_cell_that_is_not_ok_in_a_memory_process_becomes_an_error(self):
        # Ok in the timed process; the guard then says differs in one memory process, and a crash restart gives an
        # error in another. A median over the processes that happened to work must not be published.
        for later in (bad("a"), {**bad("a"), "status": "error", "detail": "the process died"}):
            cells = [ok("a", 1000), ok("b", 2000)]
            answers = iter([[ok("a", 1100), ok("b", 2100)], [later, ok("b", 2000)], [ok("a", 900), ok("b", 1900)]])

            def run_harness(*args, **kw):
                return {}, next(answers), 0
            run.run_harness = run_harness
            run.add_memory_processes({}, Path("b"), Path("w"), Path("o"), None, None, [], [], [], cells, 4,
                                     lambda *_: None)
            a, b = cells
            self.assertEqual(a["status"], "error")
            self.assertIn("not ok in memory process 3", a["detail"])
            self.assertTrue(a["detail"].rstrip().endswith(later["detail"]) or later["status"] in a["detail"])
            for key in ("time", "memory", "variant"):
                self.assertNotIn(key, a)
            self.assertEqual(b["status"], "ok")  # the other cell held up
            self.assertEqual(b["memory"]["processes"]["count"], 4)

    def test_a_cell_missing_from_a_memory_process_becomes_an_error(self):
        cells = [ok("a", 1000), ok("b", 2000)]
        self.fake([[1200, None, None], [800, 2100, None]])
        run.add_memory_processes({}, Path("b"), Path("w"), Path("o"), None, None, [], [], [], cells, 3, lambda *_: None)
        self.assertEqual(cells[1]["status"], "error")
        self.assertIn("process 2", cells[1]["detail"])
        self.assertIn("missing", cells[1]["detail"])
        self.assertEqual(cells[0]["status"], "ok")  # present in every process
        self.assertEqual(cells[0]["memory"]["processes"]["count"], 3)


def full_result(cells, declared=5, used=5, publishable=False):
    r = R_result(cells)
    r["run"]["memory_processes"] = {"csharp": {"declared": declared, "used": used}}
    r["run"]["publishability"]["publishable"] = publishable
    if publishable:
        r["run"]["publishability"]["reasons"] = []
    return r


def R_result(cells):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_results
    return test_results.result(cells, mode="full", publishable=False, reasons=("test",))


def counted(lib, n, declared=5, peak=1000, role="competitor"):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_results
    c = test_results.cell(lib, role=role, language="csharp", peak=peak)
    if n > 1:
        test_results.with_processes(c, [peak] * n)
    c["time"]["samples"] = 30
    c["memory"]["processes_declared"] = declared
    return c


class Declared(unittest.TestCase):
    def test_a_verdict_needs_both_cells_at_the_declared_count(self):
        complete = R.compare_rows([counted("ours", 5, role="own", peak=900), counted("rival", 5, peak=1000)])
        self.assertEqual(complete[0]["own"][0]["memory"]["verdict"], "win")
        # Ours at five processes against a rival measured in one (say, a rerun with the count overridden).
        short = R.compare_rows([counted("ours", 5, role="own", peak=900), counted("rival", 1, peak=1000)])
        m = short[0]["own"][0]["memory"]
        self.assertEqual(m["verdict"], "tie")
        self.assertIn("fewer processes", m["reason"])
        both = R.compare_rows([counted("ours", 1, role="own", peak=900), counted("rival", 1, peak=1000)])
        self.assertEqual(both[0]["own"][0]["memory"]["verdict"], "tie")
        # A harness that declares nothing is unaffected.
        plain = R.compare_rows([counted("ours", 1, declared=1, role="own", peak=900),
                                counted("rival", 1, declared=1, peak=1000)])
        self.assertEqual(plain[0]["own"][0]["memory"]["verdict"], "win")

    def test_every_ok_cell_must_have_the_count_the_run_used(self):
        R.validate(full_result([counted("a", 5), counted("b", 5)]))
        with self.assertRaises(R.ResultError):
            R.validate(full_result([counted("a", 5), counted("b", 3)]))  # a cell with fewer than the run used
        with self.assertRaises(R.ResultError):
            R.validate(full_result([counted("a", 5), counted("b", 1)]))  # a cell with none
        unstamped = counted("a", 5)
        del unstamped["memory"]["processes_declared"]
        with self.assertRaises(R.ResultError):
            R.validate(full_result([unstamped]))

    def test_a_run_below_the_declared_count_is_never_publishable(self):
        low = [counted("a", 1), counted("b", 1)]
        R.validate(full_result(low, used=1))  # a smoke result is allowed to be low
        with self.assertRaises(R.ResultError):
            R.validate(full_result(low, used=1, publishable=True))
        R.validate(full_result([counted("a", 5)], used=5, publishable=True))

    def test_the_driver_marks_a_run_below_the_declared_count(self):
        h = {"memory_processes": {"full": 5}}
        self.assertEqual(run.memory_process_count(h, "full", 1), 1)
        self.assertEqual(run.memory_process_count(h, "full", None), 5)


if __name__ == "__main__":
    unittest.main()
