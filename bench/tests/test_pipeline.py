"""End-to-end tests of the benchmark pipeline against the real Rust harness.

They prove the kit bites: an altered expectation turns a correct library into a `differs`
cell, a deliberately wrong library never gets a number, a crash is survived and named, and
a result built from a real run validates and renders. They need a Rust toolchain; the
selftest build with the control libraries goes into its own build directory.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from oneyaml_bench import cli, report, results, run, workloads  # noqa: E402

RUST = run.ROOT / "rust"


def build_dir() -> Path:
    env = os.environ.get("CARGO_TARGET_DIR")
    return Path(env) if env else Path(os.environ.get("KIT_OUT", run.ROOT.parent / "out")) / "bench-test-target"


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        cls.h = run.discover()["rust"]
        cls.target = build_dir()
        run.build(cls.h, cls.target)
        cls.metas = workloads.prepare(cls.out, ("quick",))
        cls.wl = cls.out / "workloads"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def harness(self, wl_dir, only, libraries=(), tasks=(), target=None, name="t"):
        out = self.out / name
        out.mkdir(exist_ok=True)
        return run.run_harness(self.h, target or self.target, wl_dir, "quick", out, None, None,
                               list(libraries), list(tasks), list(only), log=lambda *_: None)

    def result_of(self, rec, cells, metas=None):
        metas = metas or self.metas
        return run.assemble("quick", {"rust": rec}, {"rust": cells}, metas, [0.1] * 6, 0.5, 0.5, 1.0, 0, 0, None, False)

    # -- a real slice ---------------------------------------------------------------------

    def test_a_real_slice_validates_renders_and_matches_the_known_deviations(self):
        only = ["reference-a", "reference-b", "gha-workflow", "compose-merge-2k", "records-4k"]
        rec, cells, restarts = self.harness(self.wl, only, name="slice")
        self.assertEqual(restarts, 0)
        res = self.result_of(rec, cells, [m for m in self.metas if m["id"] in only])
        results.validate(res)
        md = report.render(res, self.out / "slice-report")
        self.assertIn("generic-load", md)
        self.assertEqual(cli.known_problems(res), [], "the known deviation list does not match a real run")
        by = {(c["library"], c["task"], c["workload"]): c for c in cells}
        # The expectation is confirmed by independent libraries: they all read the real file the same way.
        for lib in ("oneyaml", "serde_yaml_ng", "saphyr"):
            self.assertEqual(by[(lib, "generic-load", "gha-workflow")]["status"], "ok", lib)
        for c in cells:
            if c["status"] != "ok":
                self.assertNotIn("time", c)
                self.assertNotIn("memory", c)
            else:
                self.assertGreater(c["time"]["median_ns"], 0)
                self.assertGreaterEqual(c["memory"]["peak_bytes"], 0)
                self.assertGreater(c["memory"]["allocations"], 0)
        # Merge keys are an extension: whichever way a library reads them is named.
        merged = {c["library"]: c.get("variant") for c in cells
                  if c["workload"] == "compose-merge-2k" and c["task"] == "generic-load"
                  and c["status"] == "ok"}
        self.assertTrue(all(v in ("spec", "merged") for v in merged.values()), merged)

    def test_a_restyled_edit_is_a_wrong_answer_and_yaml_edit_can_edit_sequences(self):
        only = ["compose-wordpress", "gha-workflow", "records-4k"]
        _, cells, _ = self.harness(self.wl, only, ["oneyaml", "yaml-edit"], ["edit-script"], name="edits")
        by = {(c["library"], c["workload"]): c for c in cells}
        # Quoting a value that plain style holds is not the edit that was asked for.
        self.assertEqual(by[("oneyaml", "compose-wordpress")]["status"], "differs")
        self.assertIn("8080:80", by[("oneyaml", "compose-wordpress")]["detail"])
        # Removing a sequence item and editing a document whose root is a sequence are things the
        # path API does not do and the node API does; neither is a refusal of valid input.
        self.assertEqual(by[("yaml-edit", "gha-workflow")]["status"], "ok")
        self.assertEqual(by[("yaml-edit", "records-4k")]["status"], "ok")
        self.assertEqual(by[("oneyaml", "gha-workflow")]["status"], "ok")

    def test_merge_keys_do_not_hide_the_dump_row(self):
        _, cells, _ = self.harness(self.wl, ["compose-merge-2k"], [], ["dump"], name="dump")
        by = {c["library"]: c for c in cells if c["status"] != "unsupported"}
        self.assertEqual(set(by), {"oneyaml", "serde_yaml_ng", "saphyr"})
        for lib, c in by.items():
            self.assertEqual(c["status"], "ok", f"{lib}: {c}")
            self.assertEqual(c["memory"]["method"], "counting global allocator")

    def test_the_library_of_ours_fails_reference_case_b_and_the_kit_says_so(self):
        _, cells, _ = self.harness(self.wl, ["reference-a", "reference-b"], ["oneyaml"], ["edit-script"], name="refs")
        by = {c["workload"]: c for c in cells}
        self.assertEqual(by["reference-a"]["status"], "ok")
        self.assertEqual(by["reference-b"]["status"], "differs")
        self.assertIn("|-", by["reference-b"]["detail"])

    # -- the kit bites --------------------------------------------------------------------

    def copy_workloads(self, name):
        dest = self.out / name
        shutil.copytree(self.wl, dest)
        return dest

    def test_altering_an_expected_value_turns_a_correct_library_into_a_non_number(self):
        for lib in ("oneyaml", "serde_yaml_ng", "saphyr"):
            _, good, _ = self.harness(self.wl, ["reference-a"], [lib], ["generic-load"], name="good")
            self.assertEqual(good[0]["status"], "ok", lib)
        wl = self.copy_workloads("mut-value")
        p = wl / "reference-a" / "expected.json"
        v = json.loads(p.read_text(encoding="utf-8"))
        v["c"] = 14  # the file says 13
        p.write_text(json.dumps(v), encoding="utf-8")
        for lib in ("oneyaml", "serde_yaml_ng", "saphyr"):
            _, bad, _ = self.harness(wl, ["reference-a"], [lib], ["generic-load"], name="bad")
            self.assertEqual(bad[0]["status"], "differs", lib)
            self.assertIn("/c", bad[0]["detail"])
            self.assertNotIn("time", bad[0])

    def test_altering_expected_bytes_or_a_document_value_is_caught(self):
        wl = self.copy_workloads("mut-bytes")
        edit = wl / "reference-a" / "edit.expected.yaml"
        edit.write_bytes(edit.read_bytes().replace(b"test2", b"test3"))
        _, cells, _ = self.harness(wl, ["reference-a"], ["oneyaml"], ["edit-script"], name="mut-edit")
        self.assertEqual(cells[0]["status"], "differs")
        # A document must hold the expected value as well as write the input back.
        p = wl / "reference-a" / "expected.json"
        p.write_text(json.dumps({"a": 10, "b": "this is a test\n", "c": 14}), encoding="utf-8")
        _, cells, _ = self.harness(wl, ["reference-a"], ["oneyaml"], ["document-parse"], name="mut-doc")
        self.assertEqual(cells[0]["status"], "differs")
        self.assertIn("/c", cells[0]["detail"])

    def test_dropping_an_expectation_makes_the_workload_unusable_not_green(self):
        wl = self.copy_workloads("mut-missing")
        (wl / "reference-a" / "edit.expected.yaml").unlink()
        _, cells, _ = self.harness(wl, ["reference-a"], ["oneyaml"], ["edit-script"], name="mut-missing")
        self.assertEqual(cells[0]["status"], "differs")
        self.assertIn("no expected", cells[0]["detail"])

    # -- controls -------------------------------------------------------------------------

    @classmethod
    def selftest_build(cls) -> Path:
        target = Path(str(cls.target) + "-selftest")
        env = dict(os.environ, CARGO_TARGET_DIR=str(target))
        r = subprocess.run(["cargo", "build", "--release", "--locked", "--quiet", "--features", "selftest"],
                           cwd=RUST, env=env, capture_output=True, text=True)
        if r.returncode:
            raise AssertionError(r.stderr[-1500:])
        return target

    def test_a_wrong_competitor_gets_no_number_in_the_result_or_the_report(self):
        target = self.selftest_build()
        rec, cells, _ = self.harness(self.wl, ["reference-a", "gha-workflow"],
                                     ["oneyaml", "control-correct", "control-wrong", "control-reject"], [], target, "ctl")
        self.assertTrue(rec["selftest_build"])
        by = {}
        for c in cells:
            by.setdefault(c["library"], []).append(c)
        self.assertTrue(all(c["status"] in ("ok", "unsupported") for c in by["control-correct"]))
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
        # A build with controls can never be called publishable.
        res = self.result_of(rec, cells, [m for m in self.metas if m["id"] in ("reference-a", "gha-workflow")])
        self.assertFalse(res["run"]["publishability"]["publishable"])
        self.assertTrue(any("self-test" in r for r in res["run"]["publishability"]["reasons"]))
        # Show the wrong library in a report as a competitor, as a user's competitor would be.
        for c in res["cells"]:
            if c["role"] == "control":
                c["role"] = "competitor"
        md = report.render(res, self.out / "ctl-report")
        row = next(l for l in md.splitlines() if l.startswith("| `reference-a`") and "control-wrong" not in l and "differs" in l)
        self.assertIn("differs", row)
        self.assertRegex(md, r"control-wrong.*differs")

    def test_a_crashing_library_is_named_and_the_run_continues(self):
        target = self.selftest_build()
        logs = []
        rec, cells, restarts = run.run_harness(self.h, target, self.wl, "quick", self.out, None, None,
                                               ["control-crash", "control-correct"], ["generic-load"],
                                               ["reference-a", "reference-b"], log=logs.append)
        self.assertEqual(restarts, 2)
        crashed = [c for c in cells if c["library"] == "control-crash"]
        self.assertEqual(len(crashed), 2)
        for c in crashed:
            self.assertEqual(c["status"], "error")
            self.assertIn("died", c["detail"])
            self.assertNotIn("time", c)
        good = [c for c in cells if c["library"] == "control-correct"]
        self.assertEqual([c["status"] for c in good], ["ok", "ok"])

    # -- counters -------------------------------------------------------------------------

    def test_instruction_counts_are_deterministic(self):
        if not shutil.which("valgrind"):
            self.skipTest("valgrind is not installed on this machine")
        cell = {"library": "oneyaml", "task": "unchanged-emit", "workload": "reference-a"}
        a = run.count_instructions(self.h, self.target, self.wl, cell, self.out)
        b = run.count_instructions(self.h, self.target, self.wl, cell, self.out)
        self.assertIsNotNone(a)
        self.assertGreater(a, 1000)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
