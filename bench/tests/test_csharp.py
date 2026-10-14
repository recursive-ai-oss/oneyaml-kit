"""Tests of the .NET benchmark harness: its files, its self-test assembly, and the pipeline against the
real harness.

They prove the kit bites for .NET as it does for Rust: an altered expectation turns a correct library into
a `differs` cell, a deliberately wrong library never gets a number, a crash is survived and named, and a
result built from a real run validates, renders and matches the list of known deviations. The files are
checked without a .NET SDK; everything that builds or runs the harness is skipped when `dotnet` cannot be
found (`DOTNET` names it). The self-test assembly, with the control libraries, goes into its own build
directory under the same root as the measurement build.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from oneyaml_bench import cli, report, results, run, workloads  # noqa: E402

CSHARP = run.ROOT / "csharp"
DOTNET = shutil.which(os.environ.get("DOTNET", "dotnet")) or (
    os.environ.get("DOTNET") if os.environ.get("DOTNET") and Path(os.environ["DOTNET"]).exists() else None)
LIBRARIES = ["yamldotnet", "sharpyaml", "sharpyaml-model", "sharpyaml-syntax", "vyaml", "yaml2jsonnode"]


def build_root() -> Path:
    env = os.environ.get("KIT_DOTNET_ARTIFACTS")
    if env:
        return Path(env).parent / "bench-test-dotnet"
    return Path(os.environ.get("KIT_OUT", run.ROOT.parent / "out")) / "bench-test-dotnet"


@contextmanager
def env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    os.environ.update(kv)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class Files(unittest.TestCase):
    """What can be checked without a .NET SDK."""

    def test_the_harness_is_described_the_way_the_driver_reads_it(self):
        h = json.loads((CSHARP / "harness.json").read_text(encoding="utf-8"))
        self.assertEqual(h["language"], "csharp")
        self.assertEqual(h["format"], "oneyaml-kit-bench-harness/1")
        # The build directory is the first argument of both, so output never lands in the source tree.
        self.assertEqual(h["build"], ["./build.sh", "{build}"])
        self.assertEqual(h["command"], ["./run.sh", "{build}"])
        self.assertNotIn("counters", h)
        self.assertEqual(run.discover()["csharp"]["language"], "csharp")

    def test_every_library_and_the_harness_are_pinned_to_one_exact_release(self):
        proj = (CSHARP / "Bench.csproj").read_text(encoding="utf-8")
        refs = re.findall(r'<PackageReference Include="([^"]+)" Version="([^"]+)"', proj)
        names = {n for n, _ in refs}
        self.assertEqual(names, {"YamlDotNet", "SharpYaml", "VYaml", "Yaml2JsonNode", "JsonPatch.Net", "BenchmarkDotNet"})
        for name, version in refs:
            self.assertRegex(version, r"^\[\d+(\.\d+)+\]$", f"{name} is not pinned to an exact version")
        lock = json.loads((CSHARP / "packages.lock.json").read_text(encoding="utf-8"))
        direct = {n: d["resolved"] for n, d in lock["dependencies"]["net10.0"].items() if d["type"] == "Direct"}
        self.assertEqual(set(direct), names)
        for name, version in refs:
            self.assertEqual(direct[name], version.strip("[]"), name)

    def test_nothing_the_build_makes_is_in_the_source_tree(self):
        props = (CSHARP / "Directory.Build.props").read_text(encoding="utf-8")
        for folder in ("artifacts", "bin", "obj"):
            self.assertIn(f"{folder}/**", props)
        adapters = (run.ROOT.parent / "adapters" / "Directory.Build.props").read_text(encoding="utf-8")
        for folder in ("artifacts", "bin", "obj"):
            self.assertIn(f"{folder}/**", adapters)
        self.assertIn("artifacts", (CSHARP / ".gitignore").read_text(encoding="utf-8"))
        for script in ("build.sh", "run.sh"):
            text = (CSHARP / script).read_text(encoding="utf-8")
            self.assertIn("BUILD_DIR", text)
            self.assertNotIn("$dir/artifacts", text)

    def test_the_controls_are_not_compiled_into_a_measurement_build(self):
        proj = (CSHARP / "Bench.csproj").read_text(encoding="utf-8")
        self.assertIn('<Compile Remove="src/Libs/Control.cs" Condition="\'$(Selftest)\' != \'true\'" />', proj)
        for f in ("Control.cs", "SelfTests.cs"):
            self.assertTrue((CSHARP / "src" / ("Libs/" + f if f == "Control.cs" else f)).read_text(encoding="utf-8")
                            .lstrip().startswith("#if SELFTEST"), f)

    def test_the_known_deviations_are_well_formed(self):
        known = json.loads((CSHARP / "known.json").read_text(encoding="utf-8"))
        self.assertEqual(known["language"], "csharp")
        seen = set()
        for e in known["entries"]:
            self.assertGreater(len(e["reason"]), 20, e)
            self.assertIn(e["status"], ("differs", "rejects", "unjudged", "error", "ok"), e)
            self.assertEqual(e["status"] == "ok", "variant" in e, e)
            self.assertNotIn(e["cell"], seen)
            seen.add(e["cell"])
            lib, task, workload = e["cell"].split("/", 2)
            self.assertIn(lib, LIBRARIES)


@unittest.skipUnless(DOTNET, "the .NET SDK is not available (set DOTNET)")
class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        cls.h = run.discover()["csharp"]
        cls.target = build_root()
        env_ = {"DOTNET": DOTNET}
        with env(**env_):
            run.build(cls.h, cls.target)
        cls.metas = workloads.prepare(cls.out, ("quick",))
        cls.wl = cls.out / "workloads"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def harness(self, wl_dir, only, libraries=(), tasks=(), name="t", selftest=False):
        out = self.out / name
        out.mkdir(exist_ok=True)
        with env(DOTNET=DOTNET, **({"SELFTEST": "1"} if selftest else {})):
            return run.run_harness(self.h, self.target, wl_dir, "quick", out, None, None,
                                   list(libraries), list(tasks), list(only), log=lambda *_: None)

    def result_of(self, rec, cells, metas=None):
        metas = metas or self.metas
        return run.assemble("quick", {"csharp": rec}, {"csharp": cells}, metas, [0.1] * 6, 0.5, 0.5, 1.0, 0, 0, None, False)

    # -- a real slice ---------------------------------------------------------------------

    def test_a_real_slice_validates_renders_and_matches_the_known_deviations(self):
        only = ["reference-a", "reference-b", "gha-workflow", "compose-merge-2k", "records-4k", "deep-flow"]
        rec, cells, restarts = self.harness(self.wl, only, name="slice")
        self.assertEqual(restarts, 0)
        self.assertEqual({lib["id"] for lib in rec["libraries"]}, set(LIBRARIES))
        self.assertFalse(rec["selftest_build"])
        res = self.result_of(rec, cells, [m for m in self.metas if m["id"] in only])
        results.validate(res)
        md = report.render(res, self.out / "slice-report")
        self.assertIn("Best in the row", md)
        self.assertEqual(cli.known_problems(res), [], "the known deviation list does not match a real run")
        for c in cells:
            if c["status"] != "ok":
                self.assertNotIn("time", c)
                self.assertNotIn("memory", c)
                continue
            self.assertGreater(c["time"]["median_ns"], 0)
            self.assertEqual(c["time"]["samples"], 5)
            self.assertEqual(c["memory"]["method"], "GC.GetAllocatedBytesForCurrentThread")
            self.assertGreaterEqual(c["memory"]["allocated_bytes"], 0)
            self.assertGreater(c["memory"]["peak_bytes"], 0)
            self.assertIsNone(c["memory"]["allocations"])
        # Every library the guard let through read the file the same way.
        by = {(c["library"], c["task"], c["workload"]): c for c in cells}
        for lib in ("yamldotnet", "sharpyaml", "vyaml", "yaml2jsonnode"):
            self.assertEqual(by[(lib, "generic-load", "gha-workflow")]["status"], "ok", lib)
        # Merge keys are an extension: whichever way a library reads them is named.
        merged = {c["library"]: c.get("variant") for c in cells
                  if c["workload"] == "compose-merge-2k" and c["task"] == "generic-load" and c["status"] == "ok"}
        self.assertTrue(merged and all(v in ("spec", "merged") for v in merged.values()), merged)
        self.assertEqual(merged["sharpyaml"], "merged")

    def test_the_reference_cases_through_each_edit_path(self):
        _, cells, _ = self.harness(self.wl, ["reference-a", "reference-b"], ["sharpyaml-syntax", "yaml2jsonnode"],
                                   ["edit-script", "patch"], name="refs")
        by = {(c["library"], c["task"], c["workload"]): c for c in cells}
        # The syntax tree keeps every byte; the spans and the new text are worked out outside the clock.
        for w in ("reference-a", "reference-b"):
            self.assertEqual(by[("sharpyaml-syntax", "edit-script", w)]["status"], "ok", w)
        # The patch baseline writes the data again: comments and layout are gone, and it says so.
        for w in ("reference-a", "reference-b"):
            c = by[("yaml2jsonnode", "patch", w)]
            self.assertEqual(c["status"], "differs", w)
            self.assertNotIn("time", c)
            self.assertIn("byte 0", c["detail"])

    def test_a_dump_a_library_writes_wrongly_is_a_wrong_answer(self):
        _, cells, _ = self.harness(self.wl, ["reference-a", "records-4k"], ["vyaml", "yamldotnet"], ["dump"], name="dumps")
        by = {(c["library"], c["workload"]): c for c in cells}
        # A string that ends in a line break loses it; a number written plain reads back as a number.
        self.assertEqual(by[("vyaml", "reference-a")]["status"], "differs")
        self.assertIn("/b", by[("vyaml", "reference-a")]["detail"])
        self.assertEqual(by[("yamldotnet", "records-4k")]["status"], "differs")
        self.assertIn("read back", by[("yamldotnet", "records-4k")]["detail"])
        self.assertEqual(by[("yamldotnet", "reference-a")]["status"], "ok")

    def test_a_library_limit_is_a_refusal_and_not_a_crash(self):
        rec, cells, restarts = self.harness(self.wl, ["deep-flow"], ["sharpyaml", "vyaml"], ["generic-load"], name="deep")
        by = {c["library"]: c for c in cells}
        self.assertEqual(restarts, 0)
        self.assertEqual(by["sharpyaml"]["status"], "rejects")
        self.assertIn("maximum nesting depth", by["sharpyaml"]["detail"])
        self.assertEqual(by["vyaml"]["status"], "ok")

    # -- the numbers ----------------------------------------------------------------------

    def test_allocation_figures_repeat_to_the_byte(self):
        a = self.harness(self.wl, ["gha-workflow"], ["yamldotnet", "vyaml"], ["generic-load"], name="rep1")[1]
        b = self.harness(self.wl, ["gha-workflow"], ["yamldotnet", "vyaml"], ["generic-load"], name="rep2")[1]
        for x, y in zip(a, b):
            self.assertEqual(x["status"], "ok")
            self.assertEqual(x["memory"]["allocated_bytes"], y["memory"]["allocated_bytes"], x["library"])
            self.assertEqual(x["memory"]["peak_bytes"], y["memory"]["peak_bytes"], x["library"])

    def test_the_syntax_tree_gives_the_text_back_without_allocating(self):
        _, cells, _ = self.harness(self.wl, ["gha-workflow"], ["sharpyaml-syntax"], ["unchanged-emit", "document-parse"], name="tree")
        by = {c["task"]: c for c in cells}
        self.assertEqual(by["unchanged-emit"]["memory"]["allocated_bytes"], 0)
        # The tree it parsed is the starting state, so it is in the peak.
        self.assertGreater(by["unchanged-emit"]["memory"]["peak_bytes"], 9000)
        self.assertEqual(by["document-parse"]["variant"], "bytes-only")

    # -- the kit bites --------------------------------------------------------------------

    def copy_workloads(self, name):
        dest = self.out / name
        shutil.copytree(self.wl, dest)
        return dest

    def test_altering_an_expected_value_turns_a_correct_library_into_a_non_number(self):
        libs = ["yamldotnet", "sharpyaml", "vyaml", "yaml2jsonnode"]
        _, good, _ = self.harness(self.wl, ["reference-a"], libs, ["generic-load"], name="good")
        self.assertEqual([c["status"] for c in good], ["ok"] * 4)
        wl = self.copy_workloads("mut-value")
        p = wl / "reference-a" / "expected.json"
        v = json.loads(p.read_text(encoding="utf-8"))
        v["c"] = 14  # the file says 13
        p.write_text(json.dumps(v), encoding="utf-8")
        _, bad, _ = self.harness(wl, ["reference-a"], libs, ["generic-load"], name="bad")
        for c in bad:
            self.assertEqual(c["status"], "differs", c["library"])
            self.assertIn("/c", c["detail"])
            self.assertNotIn("time", c)

    def test_altering_expected_bytes_or_a_document_value_is_caught(self):
        wl = self.copy_workloads("mut-bytes")
        edit = wl / "reference-a" / "edit.expected.yaml"
        edit.write_bytes(edit.read_bytes().replace(b"test2", b"test3"))
        _, cells, _ = self.harness(wl, ["reference-a"], ["sharpyaml-syntax"], ["edit-script"], name="mut-edit")
        self.assertEqual(cells[0]["status"], "differs")
        # The syntax tree gives no value, so it is judged on bytes; a changed input byte is caught there.
        inp = wl / "reference-a" / "input.yaml"
        inp.write_bytes(inp.read_bytes().replace(b"a: 10", b"a: 11"))
        _, cells, _ = self.harness(wl, ["reference-a"], ["sharpyaml"], ["generic-load"], name="mut-in")
        self.assertEqual(cells[0]["status"], "differs")
        self.assertIn("/a", cells[0]["detail"])

    def test_dropping_an_expectation_makes_the_workload_unusable_not_green(self):
        wl = self.copy_workloads("mut-missing")
        (wl / "reference-a" / "edit.expected.yaml").unlink()
        _, cells, _ = self.harness(wl, ["reference-a"], ["sharpyaml-syntax"], ["edit-script"], name="mut-missing")
        self.assertEqual(cells[0]["status"], "differs")
        self.assertIn("no expected", cells[0]["detail"])

    # -- controls and the harness's own tests ----------------------------------------------------

    def test_the_self_tests_of_the_harness_pass(self):
        with env(DOTNET=DOTNET, SELFTEST="1"):
            run.build(self.h, self.target)
            r = subprocess.run(["./run.sh", str(self.target), "selftest"], cwd=CSHARP, capture_output=True, text=True,
                               timeout=280)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-1000:])
        self.assertRegex(r.stdout, r"(\d+) of \1 passed")
        self.assertNotIn("FAIL", r.stdout)

    def test_a_wrong_competitor_gets_no_number_in_the_result_or_the_report(self):
        with env(DOTNET=DOTNET, SELFTEST="1"):
            run.build(self.h, self.target)
        rec, cells, _ = self.harness(self.wl, ["reference-a", "gha-workflow"],
                                     ["control-correct", "control-wrong", "control-reject"], [], "ctl", selftest=True)
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

    def test_a_crashing_library_is_named_and_the_run_continues(self):
        with env(DOTNET=DOTNET, SELFTEST="1"):
            run.build(self.h, self.target)
        rec, cells, restarts = self.harness(self.wl, ["reference-a", "reference-b"], ["control-crash", "control-correct"],
                                            ["generic-load"], "crash", selftest=True)
        self.assertEqual(restarts, 2)
        crashed = [c for c in cells if c["library"] == "control-crash"]
        self.assertEqual(len(crashed), 2)
        for c in crashed:
            self.assertEqual(c["status"], "error")
            self.assertIn("died", c["detail"])
            self.assertNotIn("time", c)
        self.assertEqual([c["status"] for c in cells if c["library"] == "control-correct"], ["ok", "ok"])

    def test_a_folder_a_build_left_in_a_project_is_not_compiled_into_the_next_build(self):
        # An interrupted run, or a build made without the output directory set, leaves generated source files
        # under `artifacts`. They used to be compiled in as duplicates and break every build after it.
        stray = "this is not C# and must never be compiled\n"
        for project, script in ((CSHARP, "build.sh"), (run.ROOT.parent / "adapters" / "yamldotnet", "build.sh")):
            folder = project / "artifacts" / "obj"
            folder.mkdir(parents=True)
            (folder / "Stray.g.cs").write_text(stray, encoding="utf-8")
            try:
                with env(DOTNET=DOTNET, KIT_DOTNET_ARTIFACTS=str(self.out / "tree-artifacts")):
                    argv = ["./" + script] + ([str(self.out / "tree-build")] if project == CSHARP else [])
                    r = subprocess.run(argv, cwd=project, capture_output=True, text=True, timeout=250)
                self.assertEqual(r.returncode, 0, project.name + "\n" + r.stdout[-1500:] + r.stderr[-500:])
            finally:
                shutil.rmtree(project / "artifacts")

    def test_an_environment_without_dotnet_on_the_path_still_builds_into_the_build_directory(self):
        # The source tree stays clean: the build wrote under the build directory the driver chose.
        for folder in ("artifacts", "bin", "obj"):
            self.assertFalse((CSHARP / folder).exists(), folder)
        self.assertTrue((self.target / "dotnet" / "bin" / "Bench" / "release" / "OneYamlKit.Bench.CSharp.dll").exists())


if __name__ == "__main__":
    unittest.main()
