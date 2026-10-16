"""Self-tests for what the published text says about a run: the memory figure each language reports,
the settings the run used, the release tag of the library, the mark for text-to-text work and absolute
chart links."""

import copy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from oneyaml_bench import environment, results, run, taskstate  # noqa: E402
from oneyaml_bench import report as P  # noqa: E402
from test_readme import FULL, HERO, END, Base, full_run, real_world  # noqa: E402
from test_report import synthetic  # noqa: E402


def in_language(lang: str, **harness) -> dict:
    r = real_world(full_run(synthetic()), "w1", "w2")
    r["harnesses"] = {lang: {**r["harnesses"]["rust"], **harness}}
    for c in r["cells"]:
        c["language"] = lang
    return r


class MemoryLabel(Base):
    def test_each_language_names_the_figure_it_reports(self):
        for lang, label in (("go", "bytes per operation (B/op)"), ("csharp", "bytes per task"),
                            ("rust", "bytes per run")):
            with self.subTest(lang):
                self.run_cmd(in_language(lang))
                hero = self.block(self.readme, "bench-hero")
                full = self.block(self.docs / "performance.md", "bench-full")
                self.assertNotIn(label, hero)  # the hero is time only
                self.assertIn(label, full)
                for text in (hero, full):
                    self.assertNotIn("Peak", text)
                with tempfile.TemporaryDirectory() as d:
                    md = P.render(in_language(lang), Path(d))
                self.assertIn(label, md)
                self.assertNotIn("Peak", md)

    def test_an_unknown_language_gets_a_plain_label_not_a_peak(self):
        self.assertNotIn("eak", P.memory_label("zig"))


class TimerSettings(Base):
    OLD = "ns/op from testing.Benchmark at its default benchtime (20 ms in quick mode)"

    def method(self, result):
        self.run_cmd(result)
        return self.block(self.readme, "bench-hero")

    def test_a_full_run_does_not_describe_quick_mode(self):
        hero = self.method(in_language("go", timer=self.OLD))
        self.assertIn("at its default benchtime.", hero)
        self.assertNotIn("quick mode", hero)
        with tempfile.TemporaryDirectory() as d:
            self.assertNotIn("quick mode", P.render(in_language("go", timer=self.OLD), Path(d)))

    def test_a_quick_run_keeps_what_the_harness_said(self):
        r = in_language("go", timer=self.OLD)
        r["run"].update(mode="quick", smoke=True)
        self.assertIn("(20 ms in quick mode)", self.method(r))

    def test_the_settings_a_harness_recorded_are_printed_as_recorded(self):
        hero = self.method(in_language("go", timer="ns/op from testing.Benchmark at benchtime 1s"))
        self.assertIn("at benchtime 1s. Memory", hero)


def git(d, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True, check=True, env=env).stdout.strip()


class ReleaseTag(Base):
    def repo(self):
        d = self.dir / "lib"
        d.mkdir()
        git(d, "init", "-q")
        (d / "a").write_text("1")
        git(d, "add", "a")
        git(d, "commit", "-q", "-m", "one")
        return d, git(d, "rev-parse", "--short=12", "HEAD")

    def with_version(self, version):
        r = real_world(full_run(synthetic()), "w1", "w2")
        for l in r["harnesses"]["rust"]["libraries"]:
            if l["id"] == "ours":
                l["version"] = version
        return r

    def strip(self):
        return self.block(self.readme, "bench-hero")

    def test_the_tag_of_the_commit_is_shown_beside_it_even_when_the_checkout_moved_on(self):
        d, rev = self.repo()
        git(d, "tag", "v0.3.0")
        (d / "a").write_text("2")
        git(d, "commit", "-q", "-am", "two")
        self.run_cmd(self.with_version(rev), extra=["--library-source", str(d)])
        self.assertIn(f"`ours v0.3.0 ({rev})`", self.strip())

    def test_an_untagged_commit_shows_no_tag(self):
        d, rev = self.repo()
        self.run_cmd(self.with_version(rev), extra=["--library-source", str(d)])
        self.assertIn(f"`ours {rev}`", self.strip())
        self.assertNotIn("(" + rev + ")", self.strip())

    def test_a_tag_on_another_commit_is_not_borrowed(self):
        d, rev = self.repo()
        (d / "a").write_text("2")
        git(d, "commit", "-q", "-am", "two")
        git(d, "tag", "v0.4.0")
        self.run_cmd(self.with_version(rev), extra=["--library-source", str(d)])
        self.assertNotIn("v0.4.0", self.strip())

    def test_no_checkout_and_no_record_shows_the_version_alone(self):
        self.run_cmd(self.with_version("30e7fc4bf7b4"))
        self.assertIn("`ours 30e7fc4bf7b4`", self.strip())

    def test_a_recorded_tag_is_shown_without_a_checkout(self):
        r = self.with_version("30e7fc4bf7b4")
        r["run"]["sources"] = {"rust": {"library": "ours", "revision": "30e7fc4bf7b4", "tag": "v0.3.0"}}
        self.run_cmd(r)
        self.assertIn("`ours v0.3.0 (30e7fc4bf7b4)`", self.strip())
        with tempfile.TemporaryDirectory() as t:
            self.assertIn("ours 30e7fc4bf7b4 (v0.3.0)", P.render(r, Path(t)))
        # A record for another revision is not trusted.
        r["run"]["sources"]["rust"]["revision"] = "aaaaaaaaaaaa"
        self.run_cmd(r)
        self.assertNotIn("v0.3.0", self.strip())

    def test_a_run_records_the_tag_of_the_checkout_it_was_built_from(self):
        d, rev = self.repo()
        git(d, "tag", "v1.2.3")
        records = {"go": {"libraries": [{"id": "ours", "role": "own", "version": rev},
                                        {"id": "x", "role": "competitor", "version": rev}]}}
        old = os.environ.get("KIT_ONEYAML_GO_SRC")
        os.environ["KIT_ONEYAML_GO_SRC"] = str(d)
        try:
            self.assertEqual(run.library_sources(records), {"go": {"library": "ours", "revision": rev, "tag": "v1.2.3"}})
            os.environ["KIT_ONEYAML_GO_SRC"] = str(self.dir)  # not a repository
            self.assertEqual(run.library_sources(records), {})
        finally:
            os.environ.pop("KIT_ONEYAML_GO_SRC")
            if old is not None:
                os.environ["KIT_ONEYAML_GO_SRC"] = old

    def test_two_tags_on_one_commit_are_ambiguous(self):
        d, rev = self.repo()
        git(d, "tag", "v1.0.0")
        git(d, "tag", "v1.0.1")
        self.assertIsNone(environment.release_tag(d, rev))


class TextToText(Base):
    def marked(self, result):
        r = real_world(full_run(synthetic()), "w1", "w2")
        r["harnesses"]["rust"].update(result)
        return r

    def test_a_task_timed_from_text_marks_every_cell_of_it_ours_included(self):
        self.run_cmd(self.marked({"text_tasks": ["generic-load"]}))
        full = self.block(self.docs / "performance.md", "bench-full")
        ours = [l for l in full.split("\n") if l.startswith("| generic load | `w1`")]
        self.assertTrue(ours)
        self.assertTrue(all(" †" in l.split("|")[4] for l in ours), ours)
        dump = [l for l in full.split("\n") if l.startswith("| dump | `w1`")]
        self.assertNotIn("†", dump[0].split("|")[4])

    def test_the_mark_changes_no_verdict_and_no_number(self):
        plain = real_world(full_run(synthetic()), "w1", "w2")
        marked = self.marked({"text_tasks": ["generic-load", "dump"]})
        a = results.compare_rows(plain["cells"])
        b = results.compare_rows(taskstate.mark_text_tasks(copy.deepcopy(marked))["cells"])
        key = lambda rows: [(r["task"], r["workload"], r["own"][0]["time"]["verdict"] if r["own"][0]["time"] else None,  # noqa: E731
                             r["own"][0]["time"]["ratio"] if r["own"][0]["time"] else None) for r in rows]
        self.assertEqual(key(a), key(b))

    def test_the_report_says_it_once_and_leaves_the_column_headers_alone(self):
        with tempfile.TemporaryDirectory() as d:
            md = P.render(self.marked({"text_tasks": ["generic-load"]}), Path(d))
        self.assertIn("times every library's `generic-load` from text to text, ours included", md)
        self.assertNotIn("`ours` (text API)", md)

    def test_a_harness_that_says_nothing_has_no_text_task(self):
        self.assertEqual(taskstate.text_tasks({"name": "h", "version": "1"}), ())

    def test_the_go_harness_before_it_recorded_the_fact_is_known(self):
        self.assertEqual(taskstate.text_tasks({"name": "oneyaml-bench-go", "version": "0.2.0"}), ("patch",))
        self.assertEqual(taskstate.text_tasks({"name": "oneyaml-bench-go", "version": "0.2.1", "text_tasks": []}), ())


class ChartBase(Base):
    def test_relative_is_the_default(self):
        self.run_cmd()
        self.assertIn('srcset="docs/img/ours-hero-dark.svg"', self.block(self.readme, "bench-hero"))

    def test_a_base_makes_every_chart_link_absolute(self):
        (self.docs / "performance.md").write_bytes(f"{HERO}\n{END}\n{FULL}\n{END}\n".encode())
        base = "https://raw.githubusercontent.com/example/lib/v1.0.0/"
        self.assertEqual(self.run_cmd(extra=["--chart-base", base]), 0)
        for path in (self.readme, self.docs / "performance.md"):
            hero = self.block(path, "bench-hero")
            self.assertIn(f'srcset="{base}docs/img/ours-hero-dark.svg"', hero)
            self.assertIn(f'src="{base}docs/img/ours-hero-light.svg"', hero)
            self.assertNotIn('"docs/img', hero)
            self.assertNotIn('"../', hero)

    def test_a_base_that_is_not_a_url_is_refused_and_nothing_changes(self):
        before = self.readme.read_bytes()
        self.assertEqual(self.run_cmd(extra=["--chart-base", "docs/"]), 1)
        self.assertEqual(self.readme.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
