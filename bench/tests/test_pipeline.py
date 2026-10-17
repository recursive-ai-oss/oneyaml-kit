"""End-to-end tests of each harness through the driver.

They prove the kit bites: an altered expectation turns a correct library into a `differs` cell, a
deliberately wrong library never gets a number, a refusal is a `rejects`, a crash is survived and
named, and a result built from a real run validates and renders. Each language's class needs that
language's toolchain; the build with the control libraries goes into a directory of its own.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from oneyaml_bench import cli, report, results, run, workloads  # noqa: E402


def scratch() -> Path:
    return Path(os.environ.get("KIT_OUT", run.ROOT.parent / "out")) / "bench-test-build"


class Pipeline:
    """The tests every harness must pass; a subclass names its language and how to build with controls."""

    lang = ""

    @classmethod
    def build_with_controls(cls, target: Path) -> None:
        raise NotImplementedError

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        cls.h = run.discover()[cls.lang]
        cls.target = scratch() / cls.lang
        run.build(cls.h, cls.target)
        cls.metas = workloads.prepare(cls.out, ("quick",))
        cls.wl = cls.out / "workloads"
        cls.controls = scratch() / f"{cls.lang}-controls"
        cls.build_with_controls(cls.controls)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def harness(self, wl_dir, only, libraries=(), tasks=(), target=None):
        out = self.out / "run"
        out.mkdir(exist_ok=True)
        return run.run_harness(self.h, target or self.target, wl_dir, "quick", out,
                               list(libraries), list(tasks), list(only), log=lambda *_: None)

    def copy_workloads(self, name):
        dest = self.out / name
        shutil.copytree(self.wl, dest)
        return dest

    def libs_ok(self, wl, task, workload="reference-a"):
        """The libraries that answer the task correctly on the unaltered workload."""
        _, cells = self.harness(wl, [workload], [], [task])
        ok = [c["library"] for c in cells if c["status"] == "ok"]
        self.assertTrue(ok, f"no {self.lang} library answers {task} on {workload}")
        return ok

    # -- a real slice ---------------------------------------------------------------------

    def test_a_real_slice_validates_and_renders(self):
        rec, cells = self.harness(self.wl, ["reference-a", "gha-workflow"], [], ["generic-load", "edit-script"])
        res = run.assemble("quick", True, {self.lang: rec}, {self.lang: cells},
                           [m for m in self.metas if m["id"] in ("reference-a", "gha-workflow")], 0.5, 0.5, 1.0)
        results.validate(res)
        self.assertTrue(any(c["status"] == "ok" for c in res["cells"]))
        with tempfile.TemporaryDirectory() as t:
            md = report.render(res, Path(t))
        self.assertEqual(cli.text_hits(md), [])

    # -- the kit bites --------------------------------------------------------------------

    def test_altering_an_expected_value_turns_a_correct_library_into_a_non_number(self):
        good = self.libs_ok(self.wl, "generic-load")
        wl = self.copy_workloads("mut-value")
        p = wl / "reference-a" / "expected.json"
        v = json.loads(p.read_text(encoding="utf-8"))
        v["c"] = 14  # the file says 13
        p.write_text(json.dumps(v), encoding="utf-8")
        _, cells = self.harness(wl, ["reference-a"], good, ["generic-load"])
        self.assertEqual(sorted(c["library"] for c in cells), sorted(good))
        for c in cells:
            self.assertEqual(c["status"], "differs", c["library"])
            self.assertIn("/c", c["detail"])
            self.assertNotIn("time", c)

    def test_altering_expected_bytes_is_caught(self):
        good = self.libs_ok(self.wl, "edit-script")
        wl = self.copy_workloads("mut-bytes")
        edit = wl / "reference-a" / "edit.expected.yaml"
        edit.write_bytes(edit.read_bytes().replace(b"test2", b"test3"))
        _, cells = self.harness(wl, ["reference-a"], good, ["edit-script"])
        self.assertEqual([c["status"] for c in cells], ["differs"] * len(good))

    def test_dropping_an_expectation_makes_the_workload_unusable_not_green(self):
        good = self.libs_ok(self.wl, "edit-script")
        wl = self.copy_workloads("mut-missing")
        (wl / "reference-a" / "edit.expected.yaml").unlink()
        _, cells = self.harness(wl, ["reference-a"], good, ["edit-script"])
        for c in cells:
            self.assertEqual(c["status"], "differs", c["library"])
            self.assertIn("no expected", c["detail"])

    # -- controls -------------------------------------------------------------------------

    def test_a_wrong_library_gets_no_number_and_a_refusal_is_a_refusal(self):
        _, cells = self.harness(self.wl, ["reference-a"],
                                ["control-correct", "control-wrong", "control-reject"], [], self.controls)
        by = {}
        for c in cells:
            by.setdefault(c["library"], []).append(c)
        self.assertTrue(any(c["status"] == "ok" for c in by["control-correct"]))
        wrong = [c for c in by["control-wrong"] if c["status"] != "unsupported"]
        self.assertTrue(wrong)
        for c in wrong:
            self.assertEqual(c["status"], "differs", c)
            self.assertNotIn("time", c)
            self.assertNotIn("memory", c)
        for c in by["control-reject"]:
            self.assertIn(c["status"], ("rejects", "unsupported"))
            self.assertNotIn("time", c)

    def test_a_wrong_library_shows_as_differs_in_the_report(self):
        rec, cells = self.harness(self.wl, ["reference-a"], ["control-correct", "control-wrong"], ["generic-load"],
                                  self.controls)
        res = run.assemble("quick", True, {self.lang: rec}, {self.lang: cells},
                           [m for m in self.metas if m["id"] == "reference-a"], 0.5, 0.5, 1.0)
        for c in res["cells"]:
            if c["role"] == "control":
                c["role"] = "competitor"  # a control is hidden from reports; show it as a user's competitor would be
        with tempfile.TemporaryDirectory() as t:
            md = report.render(res, Path(t))
        self.assertRegex(md, r"control-wrong.*differs")

    def test_a_crashing_library_is_named_and_the_run_continues(self):
        logs = []
        _, cells = run.run_harness(self.h, self.controls, self.wl, "quick", self.out,
                                   ["control-crash", "control-correct"], ["generic-load"],
                                   ["reference-a", "reference-b"], log=logs.append)
        crashed = [c for c in cells if c["library"] == "control-crash"]
        self.assertEqual(len(crashed), 2)
        for c in crashed:
            self.assertEqual(c["status"], "error")
            self.assertIn("died", c["detail"])
            self.assertNotIn("time", c)
        self.assertEqual([c["status"] for c in cells if c["library"] == "control-correct"], ["ok", "ok"])


class RustPipeline(Pipeline, unittest.TestCase):
    lang = "rust"

    @classmethod
    def build_with_controls(cls, target):
        r = subprocess.run(["cargo", "build", "--release", "--locked", "--quiet", "--features", "selftest"],
                           cwd=run.ROOT / "rust", env=dict(os.environ, CARGO_TARGET_DIR=str(target)),
                           capture_output=True, text=True)
        if r.returncode:
            raise AssertionError(r.stderr[-1500:])


class GoPipeline(Pipeline, unittest.TestCase):
    lang = "go"

    @classmethod
    def build_with_controls(cls, target):
        target.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["go", "build", "-tags", "selftest", "-o", str(target / "oneyaml-bench-go"), "."],
                           cwd=run.ROOT / "go", capture_output=True, text=True)
        if r.returncode:
            raise AssertionError(r.stderr[-1500:])


class CSharpPipeline(Pipeline, unittest.TestCase):
    lang = "csharp"

    @classmethod
    def build_with_controls(cls, target):
        r = subprocess.run(["./build.sh", str(target)], cwd=run.ROOT / "csharp",
                           env=dict(os.environ, SELFTEST="1"), capture_output=True, text=True)
        if r.returncode:
            raise AssertionError((r.stdout + r.stderr)[-1500:])

    def harness(self, wl_dir, only, libraries=(), tasks=(), target=None):
        # run.sh reads SELFTEST to pick the build with the controls.
        with mock.patch.dict(os.environ, {"SELFTEST": "1"} if target == self.controls else {}):
            return super().harness(wl_dir, only, libraries, tasks, target)

    def test_a_crashing_library_is_named_and_the_run_continues(self):
        with mock.patch.dict(os.environ, {"SELFTEST": "1"}):
            super().test_a_crashing_library_is_named_and_the_run_continues()

    def test_a_full_run_survives_a_crashing_cell_without_a_restart(self):
        # In full mode each cell runs in a process of its own, so the process that dies is the cell's and the
        # run that collects the results goes on by itself: exit status 0 and no restart by the driver.
        out = self.out / "full-isolated.jsonl"
        r = subprocess.run([str(run.ROOT / "csharp" / "run.sh"), str(self.controls), "run", "--workloads", str(self.wl),
                            "--mode", "full", "--out", str(out), "--library", "control-crash", "--library", "control-correct",
                            "--task", "generic-load", "--workload", "reference-a"],
                           env=dict(os.environ, SELFTEST="1"), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        cells = {c["library"]: c for c in map(json.loads, out.read_text(encoding="utf-8").splitlines()) if c["type"] == "cell"}
        self.assertEqual(cells["control-crash"]["status"], "error")
        self.assertIn("the process died", cells["control-crash"]["detail"])
        self.assertEqual(cells["control-correct"]["status"], "ok")
        self.assertIn("process of its own", json.loads(out.read_text(encoding="utf-8").splitlines()[0])["timer"])

    def test_the_harness_own_checks_pass(self):
        # The checks inside the harness: the guard, the reference parsers, the edit renderer and
        # the one that fails when two prepared states share an input.
        r = subprocess.run([str(run.ROOT / "csharp" / "run.sh"), str(self.controls), "selftest"],
                           env=dict(os.environ, SELFTEST="1"), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-500:])
        self.assertNotIn("FAIL", r.stdout)
        self.assertIn("no two prepared states share an input", r.stdout)


class Pins(unittest.TestCase):
    def test_every_library_is_pinned_to_one_exact_release(self):
        cargo = (run.ROOT / "rust" / "Cargo.toml").read_text(encoding="utf-8")
        deps = cargo.split("[dependencies]")[1].split("[profile")[0]
        for line in deps.splitlines():
            if "=" in line and not line.startswith("#"):
                self.assertIn('"=', line, line)
        csproj = (run.ROOT / "csharp" / "Bench.csproj").read_text(encoding="utf-8")
        for line in csproj.splitlines():
            if "<PackageReference" in line:
                self.assertRegex(line, r'Version="\[[^\]]+\]"', line)


if __name__ == "__main__":
    unittest.main()
