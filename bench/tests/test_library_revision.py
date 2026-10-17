"""The revision a result names for our own libraries.

A source tree without git history, or a checkout with uncommitted changes, must not be recorded as if it
were a clean commit; a full run refuses it unless the revision is supplied, and a supplied revision is
marked as such. Libraries and builds are stubs: no real library is built here.
"""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from oneyaml_bench import cli, report, results, run  # noqa: E402
from oneyaml_kit import report as conformance_report  # noqa: E402

KIT = Path(__file__).resolve().parents[2]
SCRIPT = KIT / "tools" / "library-rev.sh"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]


def tmpdir() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or None)


def make_tree(root: Path, files=("go.mod", "src/OneYaml/OneYaml.csproj")) -> Path:
    for f in files:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("module stub\n")
    return root


def make_checkout(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    make_tree(root)
    subprocess.run([*GIT, "init", "-q"], cwd=root, check=True)
    subprocess.run([*GIT, "add", "."], cwd=root, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "x"], cwd=root, check=True)
    return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=root, capture_output=True,
                          text=True, check=True).stdout.strip()


def clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("KIT_ONEYAML_")}
    env.update(extra)
    return env


def library_rev(lang: str, src: Path, **extra) -> str:
    r = subprocess.run(["sh", str(SCRIPT), lang, str(src)], capture_output=True, text=True, env=clean_env(**extra))
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def library_rev_refused(lang: str, src: Path, **extra) -> str:
    r = subprocess.run(["sh", str(SCRIPT), lang, str(src)], capture_output=True, text=True, env=clean_env(**extra))
    assert r.returncode != 0 and not r.stdout, (r.returncode, r.stdout)
    return r.stderr


class Rule(unittest.TestCase):
    def test_export_without_history_is_devel_and_unknown(self):
        with tmpdir() as d:
            self.assertEqual(library_rev("GO", make_tree(Path(d))), "devel unknown")

    def test_supplied_revision_is_recorded_as_given(self):
        with tmpdir() as d:
            self.assertEqual(library_rev("GO", make_tree(Path(d)), KIT_ONEYAML_GO_REV="v1.2.3+abc"),
                             "v1.2.3+abc supplied")

    def test_supplied_revision_on_a_clean_checkout_is_only_a_check(self):
        with tmpdir() as d:
            sha = make_checkout(Path(d))
            self.assertEqual(library_rev("CSHARP", Path(d), KIT_ONEYAML_CSHARP_REV=sha[:9]), f"{sha} git")

    def test_supplied_revision_that_is_not_the_commit_is_refused(self):
        with tmpdir() as d:
            make_checkout(Path(d))
            self.assertIn("not the commit", library_rev_refused("CSHARP", Path(d), KIT_ONEYAML_CSHARP_REV="abc1234"))

    def test_supplied_revision_cannot_hide_a_dirty_checkout(self):
        with tmpdir() as d:
            sha = make_checkout(Path(d))
            (Path(d) / "go.mod").write_text("module changed\n")
            self.assertIn("uncommitted", library_rev_refused("GO", Path(d), KIT_ONEYAML_GO_REV=sha))

    def test_a_supplied_short_prefix_is_refused(self):
        with tmpdir() as d:
            sha = make_checkout(Path(d))
            self.assertIn("too short", library_rev_refused("GO", Path(d), KIT_ONEYAML_GO_REV=sha[:3]))

    def test_supplied_devel_or_modified_is_refused(self):
        with tmpdir() as d:
            make_tree(Path(d))
            for bad in ("devel", "abc1234+modified"):
                self.assertIn("not a commit", library_rev_refused("GO", Path(d), KIT_ONEYAML_GO_REV=bad))

    def test_symlinked_source_reads_its_commit(self):
        with tmpdir() as d:
            sha = make_checkout(Path(d) / "real")
            (Path(d) / "link").symlink_to(Path(d) / "real")
            self.assertEqual(library_rev("GO", Path(d) / "link"), f"{sha} git")

    def test_git_that_cannot_say_reads_as_modified(self):
        with tmpdir() as d, tmpdir() as t:
            sha = make_checkout(Path(d))
            real = subprocess.run(["sh", "-c", "command -v git"], capture_output=True, text=True).stdout.strip()
            shim = Path(t) / "git"
            shim.write_text(f'#!/bin/sh\ncase " $*" in *" status"*) exit 1;; esac\nexec {real} "$@"\n')
            shim.chmod(0o755)
            r = subprocess.run(["sh", str(SCRIPT), "GO", d], capture_output=True, text=True,
                               env=clean_env(PATH=f"{t}{os.pathsep}{os.environ['PATH']}"))
            self.assertEqual(r.stdout.strip(), f"{sha}+modified git")

    def test_a_revision_with_spaces_is_refused(self):
        with tmpdir() as d:
            r = subprocess.run(["sh", str(SCRIPT), "GO", d], capture_output=True, text=True,
                               env=clean_env(KIT_ONEYAML_GO_REV="a b"))
            self.assertNotEqual(r.returncode, 0)

    def test_clean_checkout_names_its_commit(self):
        with tmpdir() as d:
            sha = make_checkout(Path(d))
            self.assertEqual(library_rev("GO", Path(d)), f"{sha} git")

    def test_tracked_change_adds_modified(self):
        with tmpdir() as d:
            sha = make_checkout(Path(d))
            (Path(d) / "go.mod").write_text("module changed\n")
            self.assertEqual(library_rev("GO", Path(d)), f"{sha}+modified git")

    def test_untracked_file_counts(self):
        with tmpdir() as d:
            sha = make_checkout(Path(d))
            (Path(d) / "extra.go").write_text("package x\n")
            self.assertEqual(library_rev("GO", Path(d)), f"{sha}+modified git")

    def test_ignored_file_does_not_count(self):
        with tmpdir() as d:
            sha = make_checkout(Path(d))
            (Path(d) / ".git" / "info" / "exclude").write_text("*.log\n")
            (Path(d) / "build.log").write_text("x")
            self.assertEqual(library_rev("GO", Path(d)), f"{sha} git")


def stub_tool(dir_: Path, name: str) -> Path:
    """A stand-in compiler that records its arguments and succeeds."""
    p = dir_ / name
    p.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$STUB_ARGS"\n')
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


class BuildScripts(unittest.TestCase):
    """Each build script stamps what the rule says into the compiler's arguments."""

    def stamped(self, script: str, lang: str, src: Path, cwd: str, **extra) -> str:
        with tmpdir() as t:
            t = Path(t)
            go, dotnet = stub_tool(t, "go"), stub_tool(t, "dotnet")
            args = t / "args"
            env = clean_env(PATH=f"{t}{os.pathsep}{os.environ['PATH']}", STUB_ARGS=str(args), DOTNET=str(dotnet),
                            KIT_GO_BIN=str(t / "gobin"), **{f"KIT_ONEYAML_{lang}_SRC": str(src)}, **extra)
            cmd = ["sh", str(KIT / script)] + ([str(t / "build")] if script.startswith("bench/") else [])
            r = subprocess.run(cmd, cwd=KIT / cwd, env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(go and args.exists(), "the stub compiler was not called")
            return args.read_text()

    CASES = [("adapters/oneyaml-go/build.sh", "GO", "adapters/oneyaml-go", "main.version="),
             ("bench/go/build.sh", "GO", "bench/go", "main.sourceRevision="),
             ("adapters/oneyaml-csharp/build.sh", "CSHARP", "adapters/oneyaml-csharp", "OneYamlRevision="),
             ("bench/csharp/build.sh", "CSHARP", "bench/csharp", "OneYamlRevision=")]

    def test_export_is_devel(self):
        for script, lang, cwd, key in self.CASES:
            with self.subTest(script), tmpdir() as d:
                self.assertIn(f"{key}devel", self.stamped(script, lang, make_tree(Path(d)), cwd))

    def test_supplied_revision_is_stamped(self):
        for script, lang, cwd, key in self.CASES:
            with self.subTest(script), tmpdir() as d:
                out = self.stamped(script, lang, make_tree(Path(d)), cwd, **{f"KIT_ONEYAML_{lang}_REV": "abc123"})
                self.assertIn(f"{key}abc123", out)

    def test_dirty_checkout_is_stamped_modified(self):
        for script, lang, cwd, key in self.CASES:
            with self.subTest(script), tmpdir() as d:
                sha = make_checkout(Path(d))
                (Path(d) / "go.mod").write_text("module changed\n")
                self.assertIn(f"{key}{sha}+modified", self.stamped(script, lang, Path(d), cwd))


def stub_harness(version: str) -> dict:
    return {"type": "harness", "tool": "stub", "runtime": "stub", "target": "stub", "memory": "stub",
            "libraries": [{"id": "oneyaml-go", "role": "own", "package": "oneyaml", "version": version,
                           "settings": {}}]}


class Driver(unittest.TestCase):
    """run.run with the build and the harness replaced by stubs."""

    def run_mode(self, mode, out, src, stamped=None, **env):
        h = {"language": "go", "_dir": out, "build": [], "command": []}
        patches = [mock.patch.dict(os.environ, clean_env(KIT_ONEYAML_GO_SRC=str(src), **env), clear=True),
                   mock.patch.object(run, "discover", return_value={"go": h}),
                   mock.patch.object(run, "build"),
                   mock.patch.object(run.workloads, "prepare", return_value=[]),
                   mock.patch.object(run, "run_harness", side_effect=lambda *a, **k: (stub_harness(
                       stamped or run.library_revisions(["go"])["go"]["revision"]), []))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return run.run(mode, out, ["go"], log=lambda *_: None)

    def test_full_run_of_an_export_refuses(self):
        with tmpdir() as d, tmpdir() as o:
            with self.assertRaises(run.HarnessError) as e:
                self.run_mode("full", Path(o), make_tree(Path(d)))
            self.assertIn("go: devel", str(e.exception))
            self.assertIn("KIT_ONEYAML_GO_REV=<commit>", str(e.exception))

    def test_full_run_of_a_dirty_checkout_refuses(self):
        with tmpdir() as d, tmpdir() as o:
            make_checkout(Path(d))
            (Path(d) / "go.mod").write_text("module changed\n")
            with self.assertRaises(run.HarnessError) as e:
                self.run_mode("full", Path(o), Path(d))
            self.assertIn("+modified", str(e.exception))

    def test_full_run_with_the_override_records_it_as_supplied(self):
        with tmpdir() as d, tmpdir() as o:
            result = self.run_mode("full", Path(o), make_tree(Path(d)), KIT_ONEYAML_GO_REV="0123456789ab")
            self.assertEqual(result["run"]["library_revisions"],
                             {"go": {"revision": "0123456789ab", "origin": "supplied"}})
            text = "\n".join(report.revision_lines(result["run"]))
            self.assertIn("supplied by the person who ran the benchmark", text)
            self.assertIn("0123456789ab", text)

    def test_full_run_of_a_clean_checkout_is_accepted(self):
        with tmpdir() as d, tmpdir() as o:
            sha = make_checkout(Path(d))
            result = self.run_mode("full", Path(o), Path(d))
            self.assertEqual(result["run"]["library_revisions"], {"go": {"revision": sha, "origin": "git"}})

    def test_quick_run_of_an_export_works_and_says_unknown(self):
        with tmpdir() as d, tmpdir() as o:
            result = self.run_mode("quick", Path(o), make_tree(Path(d)))
            self.assertEqual(result["run"]["library_revisions"], {"go": {"revision": "devel", "origin": "unknown"}})
            self.assertIn("unknown (`devel`", "\n".join(report.revision_lines(result["run"])))

    def test_full_run_refuses_when_the_build_stamped_another_revision(self):
        with tmpdir() as d, tmpdir() as o:
            make_checkout(Path(d))
            with self.assertRaises(run.HarnessError) as e:
                self.run_mode("full", Path(o), Path(d), stamped="deadbeefdead")
            self.assertIn("deadbeefdead", str(e.exception))

    def test_smoke_full_run_of_an_export_works(self):
        with tmpdir() as d, tmpdir() as o:
            self.run_mode("full", Path(o), make_tree(Path(d)), KIT_SMOKE="1")

    def test_the_command_reports_the_refusal(self):
        with tmpdir() as d, tmpdir() as o:
            with mock.patch.dict(os.environ, clean_env(KIT_ONEYAML_GO_SRC=str(make_tree(Path(d)))), clear=True), \
                 mock.patch.object(run, "discover", return_value={"go": {"language": "go", "_dir": Path(o)}}), \
                 mock.patch.object(run.workloads, "prepare", return_value=[]), \
                 mock.patch("sys.stderr"):
                self.assertEqual(cli.main(["run", "--mode", "full", "--out", o]), 2)

    def test_merge_refuses_two_revisions_of_one_language(self):
        def part(rev):
            return {"run": {"seconds": 1, "load": {"before": 0, "after": 0},
                            "library_revisions": {"go": {"revision": rev, "origin": "git"}}},
                    "harnesses": {}, "cells": []}
        with self.assertRaises(results.ResultError):
            results.merge([part("aaaaaaaaaaaa"), part("bbbbbbbbbbbb")])
        results.merge([part("aaaaaaaaaaaa"), part("aaaaaaaaaaaa")])

    def test_published_pages_say_when_the_revision_was_supplied(self):
        from oneyaml_bench import readme
        ctx = mock.Mock(spec=readme.Ctx)
        ctx.harness = stub_harness("0123456789ab")
        ctx.library, ctx.lang, ctx.tag = "oneyaml-go", "go", None
        ctx.run = {"library_revisions": {"go": {"revision": "0123456789ab", "origin": "supplied"}}}
        self.assertIn("revision supplied", readme.Ctx.version_of(ctx, "oneyaml-go"))
        ctx.run = {"library_revisions": {"go": {"revision": "0123456789ab", "origin": "git"}}}
        self.assertNotIn("supplied", readme.Ctx.version_of(ctx, "oneyaml-go"))

    def test_merge_keeps_revisions_of_every_language(self):
        a = {"run": {"seconds": 1, "load": {"before": 0, "after": 0}, "library_revisions": {"go": {"revision": "a", "origin": "git"}}},
             "harnesses": {}, "cells": []}
        b = {"run": {"seconds": 1, "load": {"before": 0, "after": 0}, "library_revisions": {"csharp": {"revision": "b", "origin": "supplied"}}},
             "harnesses": {}, "cells": []}
        self.assertEqual(sorted(results.merge([a, b])["run"]["library_revisions"]), ["csharp", "go"])


class ConformanceHeader(unittest.TestCase):
    def test_devel_is_not_named_as_a_version(self):
        self.assertEqual(conformance_report.version_text("devel"), "(revision unknown)")
        self.assertEqual(conformance_report.version_text("abc123+modified"), "abc123+modified (uncommitted changes)")
        self.assertEqual(conformance_report.version_text("1.2.3"), "1.2.3")


if __name__ == "__main__":
    unittest.main()
