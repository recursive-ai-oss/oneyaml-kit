"""A filtered run is never publishable: a README compares every row with its strongest competitor,
and a run that left a library, a task or a workload out cannot show that."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneyaml_bench import report as P, results as R, run  # noqa: E402

LIBS = {"go": [("ours", "own"), ("rival", "competitor"), ("weak", "competitor")],
        "rust": [("rs-ours", "own"), ("rs-rival", "competitor")]}
METAS = [
    {"id": "w1", "kind": "generated", "tier": "quick", "description": "d", "documents": 1, "input_bytes": 1,
     "tasks": ["generic-load", "dump"], "source": {}},
    {"id": "w2", "kind": "generated", "tier": "quick", "description": "d", "documents": 1, "input_bytes": 1,
     "tasks": ["generic-load"], "source": {}},
]


def cell(lib, task, wl):
    return {"type": "cell", "library": lib, "task": task, "workload": wl, "status": "ok", "input_bytes": 1,
            "time": {"median_ns": 1.0, "mad_ns": 0.0, "samples": 31},
            "memory": {"method": "m", "peak_bytes": 1, "allocated_bytes": 1, "allocations": 1}}


def fake_run_harness(h, build_dir, wl_dir, mode, out_dir, sampler, pin_cpu, libraries, tasks, only, log,
                     skip_cells=()):
    """Behaves like a real harness: the record lists the chosen libraries, and only chosen cells exist."""
    lang = h["language"]
    libs = [(i, r) for i, r in LIBS[lang] if not libraries or i in libraries]
    rec = {"type": "harness", "name": "h", "version": "1", "language": lang, "runtime": "rt", "target": "t",
           "flags": {}, "config": {"warmup_ms": 1, "samples": 5, "sample_target_ms": 1}, "timer": "t", "memory": "m",
           "libraries": [{"id": i, "package": i, "version": "1", "role": r, "settings": {}} for i, r in libs]}
    cells = [cell(i, t, m["id"]) for m in METAS if not only or m["id"] in only
             for t in m["tasks"] if not tasks or t in tasks for i, _ in libs]
    return rec, cells, 0


class FakeSampler:
    def start(self): pass
    def watch(self, pid): pass
    def stop(self): return [0.1] * 10


def go(mode="full", languages=("go",), **kw):
    harnesses = {l: {"language": l, "_dir": Path(".")} for l in LIBS}
    env = {"date": "2026-01-01T00:00:00Z", "governor": "performance", "pinned_cpu": 2, "cpu": "Test CPU",
           "cpus": 4, "memory_gb": 8.0, "os": "Linux", "machine": "x86_64", "boost": "disabled",
           "kit_revision": "abc"}
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(run, "discover", return_value=harnesses), \
            mock.patch.object(run, "build"), \
            mock.patch.object(run, "run_harness", fake_run_harness), \
            mock.patch.object(run.workloads, "prepare", return_value=[dict(m) for m in METAS]), \
            mock.patch.object(run.environment, "Sampler", FakeSampler), \
            mock.patch.object(run.environment, "loadavg", return_value=(0.5, 0.5, 0.5)), \
            mock.patch.object(run.environment, "describe", return_value=dict(env)), \
            mock.patch.object(run.environment, "git_revision", return_value="abc"):
        return run.run(mode, Path(tmp), list(languages), pin_cpu=2, memory_processes=1,
                       log=lambda *_: None, **kw)


def pub(res):
    return res["run"]["publishability"]


class Selection(unittest.TestCase):
    def test_an_unfiltered_run_stays_publishable(self):
        res = go()
        self.assertTrue(pub(res)["publishable"], pub(res)["reasons"])
        sel = res["run"]["selection"]
        self.assertEqual((sel["libraries"], sel["tasks"], sel["workloads"]), ("all", "all", "all"))
        self.assertEqual(sel["languages"], ["go"])
        self.assertEqual(sel["left_out"], {"libraries": [], "tasks": [], "workloads": []})
        R.validate(res)

    def test_a_run_of_all_languages_is_publishable_too(self):
        res = go(languages=())
        self.assertTrue(pub(res)["publishable"], pub(res)["reasons"])
        self.assertEqual(res["run"]["selection"]["languages"], "all")

    def test_a_library_filter_that_drops_a_competitor_is_not_publishable(self):
        # Our library plus a weaker competitor: the strongest one is missing.
        res = go(libraries=["ours", "weak"])
        self.assertFalse(pub(res)["publishable"])
        [reason] = [r for r in pub(res)["reasons"] if "left out" in r]
        self.assertIn("go/rival", reason)
        self.assertEqual(res["run"]["selection"]["libraries"], ["ours", "weak"])
        self.assertEqual(res["run"]["selection"]["left_out"]["libraries"], ["go/rival"])

    def test_a_library_filter_naming_every_library_leaves_nothing_out(self):
        res = go(libraries=["ours", "rival", "weak"])
        self.assertTrue(pub(res)["publishable"], pub(res)["reasons"])

    def test_a_task_filter_is_not_publishable(self):
        res = go(tasks=["generic-load"])
        self.assertFalse(pub(res)["publishable"])
        self.assertTrue([r for r in pub(res)["reasons"] if "left out" in r and "dump" in r])
        self.assertEqual(res["run"]["selection"]["left_out"]["tasks"], ["dump"])

    def test_a_workload_filter_is_not_publishable(self):
        res = go(only=["w1"])
        self.assertFalse(pub(res)["publishable"])
        self.assertTrue([r for r in pub(res)["reasons"] if "left out" in r and "w2" in r])
        self.assertEqual(res["run"]["selection"]["left_out"]["workloads"], ["w2"])

    def test_validate_refuses_a_publishable_label_on_a_narrowed_run(self):
        res = go()
        res["run"]["selection"]["left_out"]["workloads"] = ["w2"]
        with self.assertRaises(R.ResultError):
            R.validate(res)


class ReportShowsIt(unittest.TestCase):
    def test_the_evidence_page_names_the_selection_and_the_reason(self):
        res = go(libraries=["ours", "weak"])
        with tempfile.TemporaryDirectory() as tmp:
            md = P.render(res, Path(tmp))
        self.assertIn("Selection: languages go; libraries ours, weak; tasks all; workloads all.", md)
        self.assertIn("left out 1 libraries (go/rival)", md)


if __name__ == "__main__":
    unittest.main()
