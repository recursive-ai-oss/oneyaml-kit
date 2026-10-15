"""End-to-end tests of the benchmark pipeline against the real Go harness.

They prove the kit bites for Go as it does for Rust: an altered expectation turns a correct
library into a cell without a number, a deliberately wrong library never gets a number, a
crash is survived and named, and a result built from a real run validates and renders. They
need a Go toolchain and the modules in its cache; the self-test build with the control
libraries goes into its own build directory.
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

GO = run.ROOT / "go"
METHOD = "runtime.MemStats, sampled"


def build_dir() -> Path:
    env = os.environ.get("CARGO_TARGET_DIR")
    base = Path(env) if env else Path(os.environ.get("KIT_OUT", run.ROOT.parent / "out")) / "bench-test-target"
    return base.parent / (base.name + "-go")


def source_built_ids(harness: dict) -> set:
    """The libraries that are compiled in only from a local checkout (harness.json
    `optional_libraries`). They are not in go.mod, so the module file cannot pin them."""
    return {lib for libs in harness.get("optional_libraries", {}).values() for lib in libs}


def check_library_pins(case: unittest.TestCase, libraries: list, pins: str, source_built: set) -> None:
    """Every competitor's version is one the module file pins. A source-built library has no pin;
    it must name the revision of the checkout it was built from instead, and it is our own."""
    for lib in libraries:
        if lib["id"] in source_built:
            case.assertTrue(lib["version"], f"{lib['id']} records no source revision")
            case.assertNotIn(lib["version"], ("", "unknown"), lib)
            case.assertEqual(lib["role"], "own", lib)
        else:
            case.assertIn(lib["version"], pins, lib)
            case.assertEqual(lib["role"], "competitor", lib)


class PinCheck(unittest.TestCase):
    """The pin check itself, on made-up libraries: it must keep biting for the competitors."""

    PINS = "require github.com/example/yaml v1.2.3\n"

    def test_a_pinned_competitor_passes(self):
        check_library_pins(self, [{"id": "yaml", "version": "v1.2.3", "role": "competitor"}], self.PINS, set())

    def test_a_competitor_whose_version_is_not_pinned_fails(self):
        with self.assertRaises(AssertionError):
            check_library_pins(self, [{"id": "yaml", "version": "v9.9.9", "role": "competitor"}], self.PINS, set())

    def test_a_competitor_is_not_exempt_because_another_library_is(self):
        libs = [{"id": "ours", "version": "abc123def456", "role": "own"},
                {"id": "yaml", "version": "v9.9.9", "role": "competitor"}]
        with self.assertRaises(AssertionError):
            check_library_pins(self, libs, self.PINS, {"ours"})

    def test_a_source_built_library_needs_no_pin_but_a_revision(self):
        check_library_pins(self, [{"id": "ours", "version": "abc123def456", "role": "own"}], self.PINS, {"ours"})
        with self.assertRaises(AssertionError):
            check_library_pins(self, [{"id": "ours", "version": "", "role": "own"}], self.PINS, {"ours"})

    def test_an_unlisted_library_with_no_pin_is_not_exempt(self):
        with self.assertRaises(AssertionError):
            check_library_pins(self, [{"id": "ours", "version": "abc123def456", "role": "own"}], self.PINS, set())

    def test_the_go_harness_names_its_source_built_library(self):
        self.assertEqual(source_built_ids(run.discover()["go"]), {"oneyaml-go"})


class GoPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        cls.h = run.discover()["go"]
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
        return run.assemble("quick", {"go": rec}, {"go": cells}, metas, [0.1] * 6, 0.5, 0.5, 1.0, 0, 0, None, False)

    def copy_workloads(self, name):
        dest = self.out / name
        shutil.copytree(self.wl, dest)
        return dest

    # -- a real slice ---------------------------------------------------------------------

    def test_a_real_slice_validates_renders_and_matches_the_known_deviations(self):
        only = ["reference-a", "reference-b", "gha-workflow", "compose-merge-2k", "records-4k"]
        rec, cells, restarts = self.harness(self.wl, only, name="slice")
        self.assertEqual(restarts, 0)
        self.assertFalse(rec["selftest_build"])
        self.assertEqual(rec["language"], "go")
        self.assertEqual(rec["memory"], METHOD)
        check_library_pins(self, rec["libraries"], (GO / "go.mod").read_text(), source_built_ids(self.h))
        res = self.result_of(rec, cells, [m for m in self.metas if m["id"] in only])
        results.validate(res)
        md = report.render(res, self.out / "slice-report")
        self.assertIn("generic-load", md)
        self.assertEqual(cli.known_problems(res), [], "the known deviation list does not match a real run")
        for c in cells:
            if c["status"] != "ok":
                self.assertNotIn("time", c)
                self.assertNotIn("memory", c)
            else:
                self.assertGreater(c["time"]["median_ns"], 0)
                self.assertEqual(c["memory"]["method"], METHOD)
                # A competitor's tasks always allocate. A source-built library may not: an unedited
                # document hands back the text it kept, which is no allocation at all.
                least = self.assertGreaterEqual if c["library"] in source_built_ids(self.h) else self.assertGreater
                least(c["memory"]["allocations"], 0)
                least(c["memory"]["allocated_bytes"], 0)

    def test_memory_counts_the_starting_state_and_not_the_process(self):
        # The harness holds every workload in memory. The peak must not include that: a tiny
        # document peaks at far less than a megabyte.
        _, cells, _ = self.harness(self.wl, ["reference-a", "compose-wordpress"], ["yaml-v3"],
                                   ["generic-load", "dump"], name="mem")
        by = {(c["workload"], c["task"]): c for c in cells}
        small = by[("reference-a", "generic-load")]["memory"]
        self.assertLess(small["peak_bytes"], 1 << 20)
        # The dump task starts from a parsed tree, which is held while it runs.
        dump = by[("compose-wordpress", "dump")]["memory"]
        self.assertGreater(dump["peak_bytes"], dump["allocated_bytes"] // 2)
        self.assertGreater(by[("compose-wordpress", "generic-load")]["memory"]["peak_bytes"], 4096)

    def test_the_libraries_are_judged_the_way_their_users_would_use_them(self):
        only = ["gha-workflow", "reference-a", "reference-b", "records-4k"]
        _, cells, _ = self.harness(self.wl, only, name="judge")
        by = {(c["library"], c["task"], c["workload"]): c for c in cells}
        # YAML 1.1 reads the key `on` as a boolean, so two libraries do not return the file.
        for lib in ("yaml-v2", "sigs-yaml"):
            self.assertEqual(by[(lib, "generic-load", "gha-workflow")]["status"], "differs", lib)
        for lib in ("yaml-v3", "yaml-v4", "goccy-go-yaml"):
            self.assertEqual(by[(lib, "generic-load", "gha-workflow")]["status"], "ok", lib)
        # A library that goes through JSON gives integers back as floats.
        sigs = by[("sigs-yaml", "generic-load", "records-4k")]
        self.assertEqual(sigs["status"], "differs")
        self.assertIn("(float)", sigs["detail"])
        # Reference case A: only goccy's AST keeps the layout; case B loses the `|-` header.
        self.assertEqual(by[("goccy-go-yaml", "edit-script", "reference-a")]["status"], "ok")
        b = by[("goccy-go-yaml", "edit-script", "reference-b")]
        self.assertEqual(b["status"], "differs")
        self.assertIn("this is a test", b["detail"])
        for lib in ("yaml-v3", "yaml-v4"):
            self.assertEqual(by[(lib, "edit-script", "reference-a")]["status"], "differs", lib)
        # A library with no capability says so, with the reason, and gets no number.
        c = by[("yaml-v2", "document-parse", "reference-a")]
        self.assertEqual(c["status"], "unsupported")
        self.assertTrue(c["detail"])

    def test_dumps_are_judged_by_the_core_schema_and_not_by_a_parser_s_typing(self):
        # A zip code written as a plain 05509 is the integer 5509 under the core schema: wrong, and
        # said to be wrong. A dump of mixed scalar styles, with a tab inside a plain scalar, is right.
        _, cells, _ = self.harness(self.wl, ["records-4k", "scalar-styles-32k"], ["goccy-go-yaml"], ["dump"],
                                   name="core")
        by = {c["workload"]: c for c in cells}
        self.assertEqual(by["records-4k"]["status"], "differs")
        self.assertIn("zip", by["records-4k"]["detail"])
        self.assertIn("05509", by["records-4k"]["detail"])
        self.assertEqual(by["scalar-styles-32k"]["status"], "ok")

    def test_retained_bytes_are_reported_beside_the_peak(self):
        _, cells, _ = self.harness(self.wl, ["reference-a", "records-4k"], ["yaml-v3"], ["generic-load"], name="kept")
        for c in cells:
            self.assertEqual(c["status"], "ok")
            kept = c["memory"]["retained_bytes"]
            self.assertIsInstance(kept, int)
            self.assertGreaterEqual(kept, 0)
            # A load leaves nothing behind but what the library keeps; it is far below the peak.
            self.assertLess(kept, c["memory"]["peak_bytes"])

    def test_the_text_api_libraries_are_marked_wherever_they_are_the_strongest(self):
        rec, cells, _ = self.harness(self.wl, ["reference-a"], ["palantir-yamlpatch", "json-patch"], ["patch"], name="text")
        for lib in rec["libraries"]:
            self.assertEqual(lib["settings"]["state"], "the text")
        res = self.result_of(rec, cells, [m for m in self.metas if m["id"] == "reference-a"])
        for c in res["cells"]:
            self.assertEqual(c["state"], "text")
        # Every competitor here did different work (text in, text out), so the strongest of them
        # is still the row's reference, but always with its mark, never as an unmarked best.
        rows = results.compare_rows(res["cells"])
        self.assertTrue(rows)
        for row in rows:
            if row["best_time"] is not None:
                self.assertNotEqual(row["best_time_mark"], "")

    def test_the_non_lossless_patch_baseline_is_marked_value_only(self):
        rec, cells, _ = self.harness(self.wl, ["reference-a"], ["json-patch", "palantir-yamlpatch"], ["patch"],
                                     name="vo")
        by = {c["library"]: c for c in cells}
        self.assertEqual(by["json-patch"]["status"], "ok")
        self.assertEqual(by["json-patch"]["variant"], "value-only")
        self.assertEqual(by["palantir-yamlpatch"]["status"], "differs")
        res = self.result_of(rec, cells, [m for m in self.metas if m["id"] == "reference-a"])
        self.assertIn("value-only", report.render(res, self.out / "vo-report"))
        # Only two libraries ran, so entries for the others are not this run's to judge.
        self.assertEqual([p for p in cli.known_problems(res) if "stale known deviation" not in p], [])

    # -- the kit bites --------------------------------------------------------------------

    def test_altering_an_expected_value_turns_a_correct_library_into_a_non_number(self):
        for lib in ("yaml-v3", "yaml-v2", "goccy-go-yaml"):
            _, good, _ = self.harness(self.wl, ["reference-a"], [lib], ["generic-load"], name="good")
            self.assertEqual(good[0]["status"], "ok", lib)
        wl = self.copy_workloads("mut-value")
        p = wl / "reference-a" / "expected.json"
        v = json.loads(p.read_text(encoding="utf-8"))
        v["c"] = 14  # the file says 13
        p.write_text(json.dumps(v), encoding="utf-8")
        for lib in ("yaml-v3", "yaml-v2", "goccy-go-yaml"):
            _, bad, _ = self.harness(wl, ["reference-a"], [lib], ["generic-load"], name="bad")
            self.assertEqual(bad[0]["status"], "differs", lib)
            self.assertIn("/c", bad[0]["detail"])
            self.assertNotIn("time", bad[0])
        # An integer in the file is not the float with the same digits.
        v["c"] = 13.0
        p.write_text(json.dumps(v), encoding="utf-8")
        _, bad, _ = self.harness(wl, ["reference-a"], ["yaml-v3"], ["generic-load"], name="float")
        self.assertEqual(bad[0]["status"], "differs")

    def test_altering_expected_bytes_is_caught_and_value_only_does_not_hide_a_wrong_value(self):
        wl = self.copy_workloads("mut-bytes")
        edit = wl / "reference-a" / "edit.expected.yaml"
        edit.write_bytes(edit.read_bytes().replace(b"test2", b"test3"))
        _, cells, _ = self.harness(wl, ["reference-a"], ["goccy-go-yaml"], ["edit-script"], name="mut-edit")
        self.assertEqual(cells[0]["status"], "differs")
        patch = wl / "reference-a" / "patch.expected.yaml"
        patch.write_bytes(patch.read_bytes().replace(b"test2", b"test3"))
        _, cells, _ = self.harness(wl, ["reference-a"], ["json-patch"], ["patch"], name="mut-patch")
        self.assertEqual(cells[0]["status"], "differs")
        self.assertIn("value after the patch", cells[0]["detail"])
        self.assertNotIn("time", cells[0])

    def test_dropping_an_expectation_makes_the_workload_unusable_not_green(self):
        wl = self.copy_workloads("mut-missing")
        (wl / "reference-a" / "edit.expected.yaml").unlink()
        _, cells, _ = self.harness(wl, ["reference-a"], ["goccy-go-yaml"], ["edit-script"], name="mut-missing")
        self.assertEqual(cells[0]["status"], "differs")
        self.assertIn("no expected", cells[0]["detail"])

    # -- controls -------------------------------------------------------------------------

    @classmethod
    def selftest_build(cls) -> Path:
        target = Path(str(cls.target) + "-selftest")
        r = subprocess.run(["go", "build", "-tags", "selftest", "-mod=readonly", "-o",
                            str(target / "oneyaml-bench-go"), "."], cwd=GO, capture_output=True, text=True)
        if r.returncode:
            raise AssertionError(r.stderr[-1500:])
        return target

    def test_a_wrong_competitor_gets_no_number_in_the_result_or_the_report(self):
        target = self.selftest_build()
        rec, cells, _ = self.harness(self.wl, ["reference-a", "gha-workflow"],
                                     ["yaml-v3", "control-correct", "control-wrong", "control-reject",
                                      "control-panic"], target=target, name="ctl")
        self.assertTrue(rec["selftest_build"])
        by = {}
        for c in cells:
            by.setdefault(c["library"], []).append(c)
        self.assertTrue(all(c["status"] == "ok" for c in by["control-correct"] if c["status"] != "unsupported"))
        wrong = [c for c in by["control-wrong"] if c["status"] != "unsupported"]
        self.assertTrue(wrong)
        for c in wrong:
            self.assertEqual(c["status"], "differs", c)
            self.assertNotIn("time", c)
            self.assertNotIn("memory", c)
        for lib in ("control-reject", "control-panic"):
            for c in by[lib]:
                self.assertIn(c["status"], ("rejects", "unsupported"), c)
                self.assertNotIn("time", c)
        # A build with controls can never be called publishable.
        res = self.result_of(rec, cells, [m for m in self.metas if m["id"] in ("reference-a", "gha-workflow")])
        self.assertFalse(res["run"]["publishability"]["publishable"])
        self.assertTrue(any("self-test" in r for r in res["run"]["publishability"]["reasons"]))

    def test_a_crashing_library_is_named_and_the_run_continues(self):
        target = self.selftest_build()
        _, cells, restarts = self.harness(self.wl, ["reference-a"], ["control-correct", "control-crash"],
                                          ["generic-load", "dump"], target=target, name="crash")
        self.assertGreaterEqual(restarts, 1)
        crashed = [c for c in cells if c["library"] == "control-crash" and c["status"] == "error"]
        self.assertTrue(crashed)
        self.assertNotIn("time", crashed[0])
        self.assertTrue(any(c["library"] == "control-correct" and c["task"] == "dump" and c["status"] == "ok"
                            for c in cells))


if __name__ == "__main__":
    unittest.main()
