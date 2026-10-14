"""The gate chooses what to run from what changed, and the full mode misses nothing."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import support
from oneyaml_kit import gate

ROOT = support.ROOT
OUT = Path("/nonexistent-out")


def areas(files, full=False):
    return gate.select_areas(files, ROOT, full)


class Choosing(unittest.TestCase):
    def test_a_documentation_change_runs_only_the_core(self):
        got, touched, _ = areas(["docs/case-format.md", "README.md"])
        self.assertEqual(got, ["core"])

    def test_kit_code_or_cases_also_run_the_reference_adapter(self):
        for f in ("tools/oneyaml_kit/compare.py", "suites/own/cases/edit/remove-key/case.json", "bin/kit"):
            got, touched, why = areas([f])
            self.assertEqual(got, ["core", "rust"], f)
            self.assertIsNone(touched["rust"])
            self.assertIn("reference adapter", why)

    def test_an_adapter_change_runs_its_language_and_only_that_adapter_s_checks(self):
        got, touched, _ = areas(["adapters/go-yaml-v3/main.go"])
        self.assertEqual(got, ["core", "go"])
        self.assertEqual(touched["go"], {"go-yaml-v3"})
        steps = gate.build_steps(got, touched, ROOT, OUT)
        names = [s.name for s in steps if s.area == "go"]
        self.assertIn("go-yaml-v3: gofmt, vet, test", names)
        self.assertNotIn("go-yaml-v2: gofmt, vet, test", names)
        self.assertTrue(any("kit run and checks" in n for n in names))

    def test_shared_adapter_code_runs_every_adapter_of_the_language(self):
        got, touched, _ = areas(["adapters/gokit/value.go"])
        self.assertEqual(got, ["core", "go"])
        self.assertIsNone(touched["go"])
        names = [s.name for s in gate.build_steps(got, touched, ROOT, OUT)]
        self.assertIn("gokit: gofmt, vet, test", names)
        self.assertIn("go-yaml-v2: gofmt, vet, test", names)
        got, touched, _ = areas(["adapters/dotnet-common/Host.cs"])
        self.assertEqual(got, ["core", "csharp"])
        self.assertIsNone(touched["csharp"])
        self.assertEqual(areas(["adapters/Directory.Build.props"])[0], ["core", "csharp"])

    def test_a_language_s_own_test_module_selects_that_language(self):
        self.assertEqual(areas(["tests/test_adapters_go.py"])[0], ["core", "go"])
        self.assertEqual(areas(["tests/test_adapter_dotnet.py"])[0], ["core", "csharp"])
        self.assertEqual(areas(["tests/test_adapter_oneyaml_rust.py"])[0], ["core", "rust"])

    def test_the_benchmark_harness_is_its_own_area(self):
        self.assertEqual(areas(["bench/oneyaml_bench/run.py"])[0], ["core", "bench"])
        self.assertEqual(areas(["bin/bench"])[0], ["core", "bench"])

    def test_several_changes_add_up_in_a_stable_order(self):
        got, touched, _ = areas(["adapters/vyaml/Program.cs", "adapters/go-yaml-v2/main.go", "bench/x.py"])
        self.assertEqual(got, ["core", "csharp", "go", "bench"])
        self.assertEqual(touched["go"], {"go-yaml-v2"})

    def test_the_full_mode_covers_every_area_and_every_adapter(self):
        got, touched, _ = areas([], full=True)
        ads = gate.adapter_dirs(ROOT)
        self.assertEqual(got[0], "core")
        self.assertEqual(got[-1], "bench")
        self.assertEqual(set(got) - {"core", "bench"}, {a["language"] for a in ads.values()})
        steps = gate.build_steps(got, touched, ROOT, OUT)
        text = "\n".join(f"{s.name} {' '.join(s.argv)}" for s in steps)
        for name, a in ads.items():
            if a["language"] == "go":
                self.assertIn(f"{name}: gofmt, vet, test", text, name)
            elif a["language"] == "csharp":
                self.assertIn(f"adapters/{name}/", text, name)
            elif a["language"] == "rust":
                self.assertIn(f"{name}: clippy", text, name)
        self.assertIn("benchmark quick run", text)
        self.assertIn("public text", text)

    def test_every_language_is_either_tested_or_run_by_name(self):
        for name, a in gate.adapter_dirs(ROOT).items():
            steps = gate.build_steps([a["language"]], {a["language"]: None}, ROOT, OUT)
            ran = a["language"] in gate.LANG_TESTS or any(s.name == f"{name}: kit run" for s in steps)
            self.assertTrue(ran, name)

    def test_the_hand_over_tier_leaves_other_languages_to_ci(self):
        got, touched, _ = areas(["adapters/oneyaml-rust/src/main.rs"])
        steps = gate.build_steps(got, touched, ROOT, OUT)
        self.assertEqual({s.area for s in steps}, {"core", "rust"})

    def test_adapter_tests_do_not_build_adapters_in_the_core_step(self):
        steps = gate.build_steps(["core"], {}, ROOT, OUT)
        core_tests = next(s for s in steps if s.name == "kit tests")
        self.assertIn("KIT_ADAPTER_TESTS", core_tests.drop_env)
        steps = gate.build_steps(["go"], {"go": None}, ROOT, OUT)
        self.assertEqual(steps[0].env, {"KIT_ADAPTER_TESTS": "1"})


class Running(unittest.TestCase):
    def test_a_step_records_its_exit_status_and_keeps_its_output_in_a_log(self):
        t = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, t)
        ok = gate.Step("core", "fine", [gate.py(), "-c", "print('hello')"])
        bad = gate.Step("core", "broken", [gate.py(), "-c", "import sys; print('boom'); sys.exit(3)"])
        missing = gate.Step("core", "absent", ["/no/such/tool"])
        r = [gate.run_step(s, t, t, dict(os.environ), i) for i, s in enumerate((ok, bad, missing))]
        self.assertEqual([x["rc"] for x in r], [0, 3, 127])
        self.assertEqual(r[1]["log"].read_text().strip(), "boom")
        self.assertIn("hello", r[0]["log"].read_text())

    def test_build_output_goes_to_the_output_directory_unless_told_otherwise(self):
        t = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, t)
        code = "import os; print(os.environ['CARGO_TARGET_DIR'])"
        env = {k: v for k, v in os.environ.items() if k != "CARGO_TARGET_DIR"}
        r = gate.run_step(gate.Step("core", "x", [gate.py(), "-c", code]), t, t, env, 0)
        self.assertEqual(r["log"].read_text().strip(), str(t / "target"))


class Changes(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, self.t)

    def git(self, *a):
        subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=n", *a], cwd=self.t, check=True,
                       capture_output=True)

    def test_committed_edited_and_new_files_are_all_seen(self):
        self.git("init", "-q", "-b", "main")
        (self.t / "a.txt").write_text("1")
        (self.t / "b.txt").write_text("1")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")
        self.git("checkout", "-q", "-b", "work")
        (self.t / "a.txt").write_text("2")
        self.git("commit", "-qam", "edit")
        (self.t / "b.txt").write_text("2")
        (self.t / "c.txt").write_text("new")
        self.assertEqual(gate.changed_files(self.t, "main"), ["a.txt", "b.txt", "c.txt"])

    def test_an_unknown_base_is_reported_so_the_caller_can_run_everything(self):
        self.git("init", "-q", "-b", "main")
        (self.t / "a.txt").write_text("1")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")
        self.assertIsNone(gate.changed_files(self.t, "no-such-branch"))


if __name__ == "__main__":
    unittest.main()
