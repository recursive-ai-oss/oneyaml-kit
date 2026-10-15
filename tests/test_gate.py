"""The gate chooses what to run from what changed, and the full mode misses nothing."""

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
        for f in ("suites/own/cases/edit/remove-key/case.json", "bin/kit", "tests/support.py",
                  "tests/fixtures/fake_adapter.py"):
            got, touched, why = areas([f])
            self.assertEqual(got, ["core", "rust"], f)
            self.assertIsNone(touched["rust"])
            self.assertIn("reference adapter", why)

    def test_kit_code_also_runs_the_benchmark_harness_that_imports_it(self):
        got, touched, why = areas(["tools/oneyaml_kit/compare.py"])
        self.assertEqual(got, ["core", "rust", "bench"])
        self.assertIn("benchmark harness imports it", why)
        self.assertEqual(areas(["suites/own/cases/edit/remove-key/case.json"])[0], ["core", "rust"])

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

    def test_the_benchmark_driver_is_its_own_area_and_smoke_tests_one_harness(self):
        for f in ("bench/oneyaml_bench/run.py", "bin/bench", "bench/workloads/real/x.yaml"):
            got, _, why = areas([f])
            self.assertEqual(got, ["core", "bench", "bench-rust"], f)
            self.assertIn("smoke", why)
        # Tests of the driver need no harness at all.
        self.assertEqual(areas(["bench/tests/test_results.py"])[0], ["core", "bench"])

    def test_a_harness_change_selects_that_harness_alone(self):
        self.assertEqual(areas(["bench/go/measure.go"])[0], ["core", "bench-go"])
        self.assertEqual(areas(["bench/rust/src/main.rs"])[0], ["core", "bench-rust"])
        self.assertEqual(areas(["bench/rust/Cargo.lock"])[0], ["core", "bench-rust"])
        self.assertEqual(areas(["bench/csharp/Bench.csproj"])[0], ["core", "bench-csharp"])
        self.assertEqual(areas(["bench/tests/test_pipeline.py"])[0], ["core", "bench-rust"])

    def test_a_change_to_the_oneyaml_csharp_adapter_also_runs_the_csharp_harness_area(self):
        got, _, _ = areas(["adapters/oneyaml-csharp/Lib.cs"])
        self.assertEqual(got, ["core", "csharp", "bench-csharp"])
        self.assertEqual(areas(["adapters/vyaml/Program.cs"])[0], ["core", "csharp"])

    def test_several_changes_add_up_in_a_stable_order(self):
        got, touched, _ = areas(["adapters/vyaml/Program.cs", "adapters/go-yaml-v2/main.go", "bench/x.py"])
        self.assertEqual(got, ["core", "csharp", "go", "bench", "bench-rust"])
        self.assertEqual(touched["go"], {"go-yaml-v2"})

    def test_the_full_mode_covers_every_area_and_every_adapter(self):
        got, touched, _ = areas([], full=True)
        ads = gate.adapter_dirs(ROOT)
        self.assertEqual(got[0], "core")
        self.assertEqual(got[-len(gate.AREA_NAMES_BENCH):], gate.AREA_NAMES_BENCH)
        self.assertEqual(set(got) - {"core", *gate.AREA_NAMES_BENCH}, {a["language"] for a in ads.values()})
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
        for step in ("benchmark clippy", "benchmark self-test clippy", "benchmark self-test",
                     "benchmark unit tests", "benchmark fmt", "benchmark go: gofmt, vet, test",
                     "benchmark go self-test", "benchmark .NET harness format"):
            self.assertIn(step, text)
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
        self.assertEqual(steps[0].env, {"KIT_ADAPTER_TESTS": "1", "KIT_SLOW": "1"})  # a full-tier plan
        steps = gate.build_steps(["go"], {"go": None}, ROOT, OUT, full=False)
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


def run_main(areas=None, without_core=False):
    args = argparse.Namespace(full=False, base="main", areas=areas, without_core=without_core, jobs=1, plan=True,
                              out=str(Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))))
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        rc = gate.main(args)
    shutil.rmtree(args.out)
    return rc, buf.getvalue(), err.getvalue()


class Areas(unittest.TestCase):
    def test_an_unknown_area_is_refused(self):
        rc, out, err = run_main(areas="foo")
        self.assertEqual(rc, 2)
        self.assertIn("unknown area", err)
        self.assertIn("core", err)
        self.assertEqual(run_main(areas="go,nope")[0], 2)
        self.assertEqual(run_main(areas=",")[0], 2)

    def test_the_core_is_always_included_unless_told_otherwise(self):
        rc, out, _ = run_main(areas="go")
        self.assertEqual(rc, 0)
        self.assertIn("areas core, go;", out)
        self.assertIn("areas go;", run_main(areas="go", without_core=True)[1])

    def test_the_revision_is_printed_before_the_steps(self):
        rc, out, _ = run_main(areas="core")
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("gate: revision HEAD "), lines[0])
        self.assertIn("fingerprint", lines[0])


class Revision(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, self.t)
        self.git("init", "-q", "-b", "main")
        (self.t / "a.txt").write_text("1")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")

    def git(self, *a):
        subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=n", *a], cwd=self.t, check=True,
                       capture_output=True)

    def test_the_fingerprint_follows_the_content_of_changes_and_new_files(self):
        clean = gate.revision(self.t, "main")
        self.assertTrue(clean["available"])
        self.assertFalse(clean["dirty"])
        self.assertEqual(clean["head"], clean["merge_base"])
        (self.t / "a.txt").write_text("2")
        edited = gate.revision(self.t, "main")
        self.assertTrue(edited["dirty"])
        self.assertNotEqual(clean["fingerprint"], edited["fingerprint"])
        (self.t / "new.txt").write_text("x")
        with_x = gate.revision(self.t, "main")
        (self.t / "new.txt").write_text("y")
        with_y = gate.revision(self.t, "main")
        self.assertEqual((with_x["untracked_files"], with_y["untracked_files"]), (1, 1))
        self.assertNotEqual(with_x["fingerprint"], with_y["fingerprint"])
        (self.t / "new.txt").write_text("x")
        self.assertEqual(gate.revision(self.t, "main")["fingerprint"], with_x["fingerprint"])

    def test_binary_changes_count(self):
        (self.t / "b.bin").write_bytes(b"\0\1")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "bin")
        a = gate.revision(self.t, "main")
        (self.t / "b.bin").write_bytes(b"\0\2")
        self.assertNotEqual(gate.revision(self.t, "main")["fingerprint"], a["fingerprint"])


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

    def test_a_rename_selects_the_area_it_left_as_well_as_the_one_it_entered(self):
        self.git("init", "-q", "-b", "main")
        (self.t / "adapters" / "go-yaml-v3").mkdir(parents=True)
        (self.t / "adapters" / "go-yaml-v3" / "go.mod").write_text("module x\n" * 20)
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")
        self.git("checkout", "-q", "-b", "work")
        (self.t / "tools").mkdir()
        self.git("mv", "adapters/go-yaml-v3/go.mod", "tools/go.mod.moved")
        self.git("commit", "-qam", "move")
        files = gate.changed_files(self.t, "main")
        self.assertEqual(files, ["adapters/go-yaml-v3/go.mod", "tools/go.mod.moved"])
        got, touched, _ = gate.select_areas(files, ROOT, False)
        self.assertIn("go", got)
        self.assertEqual(touched["go"], {"go-yaml-v3"})

    def test_a_missing_git_falls_back_instead_of_raising(self):
        old = os.environ["PATH"]
        os.environ["PATH"] = "/nonexistent"
        try:
            self.assertIsNone(gate.changed_files(self.t, "main"))
            self.assertEqual(gate.revision(self.t, "main"), {"available": False})
        finally:
            os.environ["PATH"] = old

    def test_an_unknown_base_is_reported_so_the_caller_can_run_everything(self):
        self.git("init", "-q", "-b", "main")
        (self.t / "a.txt").write_text("1")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")
        self.assertIsNone(gate.changed_files(self.t, "no-such-branch"))


ALL = ["core", *sorted({a["language"] for a in gate.adapter_dirs(ROOT).values()}), *gate.AREA_NAMES_BENCH]


def plan(full, areas=ALL):
    return gate.build_steps(areas, {a: None for a in areas}, ROOT, OUT, full=full)


def ident(s):
    return (s.area, s.name, tuple(s.argv), s.cwd)


class Tiers(unittest.TestCase):
    def test_the_hand_over_tier_leaves_the_slow_steps_to_the_full_tier(self):
        fast = {s.name for s in plan(False)}
        for slow in ("benchmark quick run", "benchmark clippy",
                     "benchmark rust pipeline tests", "benchmark go pipeline tests",
                     "benchmark csharp pipeline tests"):
            self.assertNotIn(slow, fast)
        for quick in ("benchmark unit tests", "benchmark rust smoke run", "benchmark self-test",
                      "benchmark go smoke run", "benchmark csharp smoke run"):
            self.assertIn(quick, fast)

    def test_every_moved_step_still_runs_in_the_full_tier(self):
        full = {s.name for s in plan(True)}
        for moved in ("benchmark quick run", "benchmark clippy",
                      "benchmark rust pipeline tests", "benchmark go pipeline tests",
                      "benchmark csharp pipeline tests", "benchmark self-test clippy", "benchmark self-test"):
            self.assertIn(moved, full)
        # The hand-over tier's only extras are quick stand-ins that the full tier's slow steps subsume.
        extra = {s.name for s in plan(False)} - full
        self.assertEqual(extra, {"benchmark rust smoke run", "benchmark go smoke run",
                                 "benchmark csharp smoke run"})

    def test_only_the_full_tier_runs_the_slow_cases(self):
        langs = [s for s in plan(True) if s.area not in ("core",) and not s.area.startswith("bench")]
        self.assertTrue(any("kit run and checks" in s.name for s in langs))
        for s in langs:
            self.assertEqual(s.env.get("KIT_SLOW"), "1", s.name)
        for s in plan(False):
            self.assertNotIn("KIT_SLOW", s.env, s.name)
        for s in plan(True):
            if s.area == "core" or s.area.startswith("bench"):
                self.assertNotIn("KIT_SLOW", s.env, s.name)

    def test_the_full_tier_runs_every_hand_over_step_that_is_not_a_stand_in(self):
        full = {ident(s) for s in plan(True)}
        for s in plan(False):
            if s.tier == "both":
                self.assertIn(ident(s), full, s.name)
            else:
                self.assertEqual(s.tier, "hand-over", s.name)

    def test_the_full_tier_runs_every_benchmark_test_module(self):
        mods = sorted(p.stem for p in (ROOT / "bench" / "tests").glob("test_*.py"))
        steps = plan(True)
        unit = next(s for s in steps if s.name == "benchmark unit tests")
        run_files = {m + ".py" for m in unit.argv[unit.argv.index("-q") + 1:]}
        for s in steps:
            if s.name.endswith("pipeline tests"):
                run_files.add(s.argv[s.argv.index("-q") + 1].split(".")[0] + ".py")
        self.assertEqual(run_files, {m + ".py" for m in mods})

    def test_the_slow_test_rule_matches_files_and_pipeline_modules(self):
        mods = [p.stem for p in (ROOT / "bench" / "tests").glob("test_*.py")]
        for frag in gate.BENCH_SLOW_TESTS:
            self.assertTrue(any(frag in m for m in mods), f"stale entry {frag!r}")
        slow = {m + ".py" for m in mods if any(f in m for f in gate.BENCH_SLOW_TESTS)}
        self.assertEqual(slow, {v.split(".")[0] + ".py" for v in gate.BENCH_PIPELINE_TESTS.values()})
        self.assertEqual(set(gate.BENCH_PIPELINE_TESTS), set(gate.BENCH_LANGS))

    def test_a_harness_area_builds_only_its_own_language(self):
        tools = {"bench-go": "go", "bench-rust": "cargo", "bench-csharp": "dotnet"}
        for area, tool in tools.items():
            for s in plan(False, [area]):
                exe = os.path.basename(s.argv[0])
                others = {t for a, t in tools.items() if a != area}
                self.assertNotIn(exe, others, f"{area}: {s.name}")
                self.assertFalse(any(f"bench/{o}" in s.cwd for o in ("rust", "go", "csharp") if o != area[6:]), s.name)
                if "--lang" in s.argv:
                    self.assertEqual(s.argv[s.argv.index("--lang") + 1], area[6:])

    def test_areas_that_run_side_by_side_build_into_separate_directories(self):
        dirs = {}
        for s in plan(False, gate.AREA_NAMES_BENCH):
            if s.name.endswith("smoke run"):
                dirs[s.area] = s.env["CARGO_TARGET_DIR"]
        self.assertEqual(len(dirs), len(gate.BENCH_LANGS))
        self.assertEqual(len(set(dirs.values())), len(dirs))

    def test_the_smoke_run_uses_small_workloads_and_a_typed_one(self):
        for w in gate.SMOKE_WORKLOADS:
            self.assertIn(w, workload_ids())
        self.assertTrue(any(w == "gha-workflow" for w in gate.SMOKE_WORKLOADS))


def workload_ids():
    sys.path.insert(0, str(ROOT / "bench"))
    from oneyaml_bench import workloads
    return set(workloads.universe(("quick",)))


class Budget(unittest.TestCase):
    def results(self):
        return [{"area": "go", "name": "slow", "seconds": 90.0}, {"area": "core", "name": "fast", "seconds": 1.0}]

    def test_a_run_over_budget_is_reported_with_its_slowest_steps(self):
        msg = gate.over_budget(200.0, 150, self.results())
        self.assertIn("200 s", msg)
        self.assertIn("budget of 150 s", msg)
        self.assertIn("go: slow (90 s)", msg)
        self.assertIsNone(gate.over_budget(150.0, 150, self.results()))
        self.assertIsNone(gate.over_budget(9999.0, None, self.results()))

    def test_the_default_budget_leaves_a_margin_under_three_minutes(self):
        self.assertLessEqual(gate.BUDGET_SECONDS, 150)

    def run_fake(self, full, budget, jobs=1, nap=0.3):
        steps = [gate.Step(a, f"nap {a}", [gate.py(), "-c", f"import time; time.sleep({nap})"]) for a in ("go", "rust")]
        args = argparse.Namespace(full=full, base="main", areas="go,rust", without_core=True, jobs=jobs, plan=False,
                                  budget=budget, out=str(Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))))
        self.addCleanup(shutil.rmtree, args.out)
        buf, err = io.StringIO(), io.StringIO()
        old = gate.build_steps
        gate.build_steps = lambda *a, **k: steps
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                t0 = time.monotonic()
                rc = gate.main(args)
                wall = time.monotonic() - t0
        finally:
            gate.build_steps = old
        summary = json.loads((Path(args.out) / "gate" / "summary.json").read_text())
        return rc, buf.getvalue(), err.getvalue(), wall, summary

    def test_the_hand_over_gate_fails_when_it_exceeds_its_budget(self):
        rc, out, err, _, summary = self.run_fake(full=False, budget=0.1)
        self.assertEqual(rc, 1)
        self.assertIn("over its budget", err)
        self.assertIn("wall-clock", out)
        self.assertTrue(summary["over_budget"])
        rc, out, err, _, summary = self.run_fake(full=False, budget=60)
        self.assertEqual(rc, 0)
        self.assertFalse(summary["over_budget"])

    def test_the_full_gate_has_no_budget(self):
        rc, out, err, _, summary = self.run_fake(full=True, budget=0.1)
        self.assertEqual(rc, 0)
        self.assertIsNone(summary["budget_seconds"])

    def test_areas_run_side_by_side_by_default_and_one_at_a_time_on_request(self):
        rc, _, _, wall_one, _ = self.run_fake(full=False, budget=60, jobs=1)
        rc, _, _, wall_two, summary = self.run_fake(full=False, budget=60, jobs=2)
        self.assertLess(wall_two, wall_one - 0.2)
        self.assertEqual(summary["jobs"], 2)
        _, _, _, _, summary = self.run_fake(full=False, budget=60, jobs=0)
        self.assertEqual(summary["jobs"], gate.default_jobs())
        self.assertTrue(1 <= gate.default_jobs() <= gate.DEFAULT_JOBS)


class Workflow(unittest.TestCase):
    """CI runs one job per area of the full gate; a job list that has drifted from the areas would
    leave an area unchecked, or ask for one that no longer exists."""

    def setUp(self):
        import re
        self.text = (ROOT / ".github" / "workflows" / "gate.yml").read_text()
        self.matrix = re.findall(r"^\s+- area: (\S+)\s*$", self.text, re.M)

    def test_every_area_of_the_gate_has_a_job_and_no_other_does(self):
        known = {"core", *(a["language"] for a in gate.adapter_dirs(ROOT).values()), *gate.AREA_NAMES_BENCH}
        self.assertEqual(sorted(self.matrix), sorted(known))
        self.assertEqual(len(set(self.matrix)), len(self.matrix))

    def test_a_job_installs_what_its_area_needs(self):
        import re
        blocks = {m.group(1): m.group(2) for m in re.finditer(
            r"- area: (\S+)\n((?:\s+(?:go|rust|dotnet): true\n)*)", self.text)}
        needs = {"go": {"go"}, "bench-go": {"go"}, "rust": {"rust"}, "bench-rust": {"rust"},
                 "csharp": {"dotnet"}, "bench-csharp": {"dotnet"}, "core": set(),
                 # The quick run of the benchmark driver builds every harness.
                 "bench": {"go", "rust", "dotnet"}}
        for area, want in needs.items():
            got = set(re.findall(r"(go|rust|dotnet): true", blocks[area]))
            self.assertEqual(got, want, area)

    def test_the_jobs_do_not_repeat_the_core(self):
        self.assertIn("--without-core", self.text)

    def test_caches_cover_every_lock_file_in_the_repository(self):
        # A lock file that no cache key reads is a dependency CI downloads on every run.
        for lock in sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "adapters").glob("*/go.sum")):
            self.assertIn("adapters/*/go.sum", self.text, lock)
        self.assertIn("bench/go/go.sum", self.text)
        self.assertIn("bench/csharp/packages.lock.json", self.text)
        self.assertIn("adapters/*/packages.lock.json", self.text)
        self.assertIn("bench/rust/Cargo.lock", self.text)
        self.assertIn("adapters/*/Cargo.lock", self.text)


if __name__ == "__main__":
    unittest.main()
