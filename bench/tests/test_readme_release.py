"""Self-tests for what a README block must be fit for when a library publishes a release from it: chart links
that stay with the tagged text, a time-only hero in the family's "N× faster" form, a chart that fits its
canvas, short alt text beside a table, and a revision that cannot be mistaken for a release tag."""

import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from oneyaml_bench import cli, environment  # noqa: E402
from oneyaml_bench import readme as M  # noqa: E402
from oneyaml_bench import report as P  # noqa: E402
from test_readme import Base, END, FULL, HERO, full_run, probe_result, real_world  # noqa: E402
from test_report import synthetic  # noqa: E402
from test_results import cell  # noqa: E402

RENDERER_REVISION = environment.renderer_revision  # before any test stands in for it
GOLDEN = Path(__file__).parent / "fixtures" / "hero_golden.md"
REVISION = "0123456789ab"


def svg_of(base, theme="light") -> str:
    return (base.dir / "docs" / "img" / f"ours-hero-{theme}.svg").read_text(encoding="utf-8")


def desc_of(svg: str) -> str:
    return svg[svg.index("<desc>"):svg.index("</desc>")]


def alt_of(hero: str) -> str:
    alt = hero[hero.index('alt="') + 5:]
    return alt[:alt.index('"')]


class Hero(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(environment, "renderer_revision", return_value=REVISION)
        p.start()
        self.addCleanup(p.stop)


class ChartLinks(Hero):
    def test_default_links_are_relative_in_every_file_and_the_docs_say_so(self):
        (self.docs / "performance.md").write_bytes(f"{HERO}\n{END}\n".encode())
        self.run_cmd()
        for path, rel in ((self.readme, "docs/img"), (self.docs / "performance.md", "img")):
            hero = self.block(path, "bench-hero")
            self.assertIn(f'srcset="{rel}/ours-hero-dark.svg"', hero)
            self.assertNotIn("http", hero.split("Method:")[0])
        text = (Path(__file__).resolve().parents[2] / "docs" / "benchmarks.md").read_text(encoding="utf-8")
        self.assertIn("never to a branch", text)

    def test_a_base_on_a_branch_is_refused_and_nothing_changes(self):
        before = self.files()
        for ref in ("main", "master", "HEAD", "refs/heads/main"):
            with self.subTest(ref):
                base = f"https://raw.githubusercontent.com/example/lib/{ref}/"
                self.assertEqual(self.run_cmd(extra=["--chart-base", base]), 1)
                self.assertEqual(self.files(), before)

    def test_the_refusal_says_what_to_do(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            res = self.put()
            cli.main(["readme", "--results", str(res), "--library", "ours", "--readme", str(self.readme),
                      "--docs", str(self.docs), "--allow-smoke", "--chart-base",
                      "https://raw.githubusercontent.com/example/lib/main"])
        self.assertIn("pin it to a tag", err.getvalue())

    def test_a_base_at_a_tag_is_accepted_even_when_the_owner_or_repository_is_called_main(self):
        base = "https://raw.githubusercontent.com/main/dev/v1.0.0"
        self.assertEqual(self.run_cmd(extra=["--chart-base", base]), 0)
        self.assertIn(f'srcset="{base}/docs/img/ours-hero-dark.svg"', self.block(self.readme, "bench-hero"))


class TimeOnly(Hero):
    HEADER = ["Task", "`ours` time (geometric mean)", "Strongest competitor", "Its time (geometric mean)",
              "Speed against it (its time ÷ ours; higher is better)", "Verdict"]

    def table(self):
        lines = [l for l in self.block(self.readme, "bench-hero").split("\n") if l.startswith("|")]
        return [[c.strip() for c in l.strip("|").split("|")] for l in lines]

    def test_columns_are_task_ours_rival_theirs_speed_verdict(self):
        self.run_cmd()
        rows = self.table()
        self.assertEqual(rows[0], self.HEADER)
        self.assertTrue(all(len(r) == 6 for r in rows))

    def test_memory_and_counts_stay_on_the_full_page(self):
        self.run_cmd()
        hero = "\n".join(l for l in self.block(self.readme, "bench-hero").split("\n") if l.startswith("|"))
        for needle in ("allocated", "better,", "worse of", "KiB", " B "):
            self.assertNotIn(needle, hero)
        self.assertIn(P.memory_label("rust"), self.block(self.docs / "performance.md", "bench-full"))

    def test_unchanged_emit_is_kept_and_says_what_it_measures(self):
        r = real_world(synthetic(), "w1")
        r["cells"] += [cell("ours", "own", task="unchanged-emit", workload="w1", t=5.0),
                       cell("rival", task="unchanged-emit", workload="w1", t=500.0)]
        self.run_cmd(r)
        row = next(l for l in self.block(self.readme, "bench-hero").split("\n") if l.startswith("| unchanged emit"))
        self.assertIn("not a rendering speed", row)
        self.assertIn("100× faster", row)
        self.assertIn("not rendering", svg_of(self))

    def test_a_measured_zero_prints_as_zero_where_memory_is_shown(self):
        r = real_world(synthetic(), "w1")
        r["cells"] = [cell("ours", "own", workload="w1", t=50.0, peak=0), cell("rival", workload="w1", t=100.0, peak=400)]
        self.run_cmd(r)
        full = self.block(self.docs / "performance.md", "bench-full")
        self.assertIn("| 0 B in 3 allocs |", full)
        self.assertNotIn("1 B", full)
        self.assertNotIn(" 1 B", self.block(self.readme, "bench-hero"))
        with tempfile.TemporaryDirectory() as d:
            md = P.render(r, Path(d))
            self.assertNotRegex(md.replace("as 1 B on", ""), r"\b1 B\b")
            self.assertIn("ours 0 B vs rival 400 B", md)
            self.assertIn("0 B is placed as 1 B on the log axis", md)


class Canvas(Hero):
    def test_every_start_aligned_text_of_the_hero_fits_the_canvas(self):
        self.run_cmd()
        for theme in ("light", "dark"):
            svg = svg_of(self, theme)
            width = int(re.search(r'<svg[^>]* width="(\d+)"', svg).group(1))
            for m in re.finditer(r'<text x="([\d.]+)"([^>]*)>(.*?)</text>', svg):
                if "text-anchor" in m.group(2):
                    continue
                size = float(re.search(r'font-size="(\d+)"', m.group(2)).group(1))
                lines = re.findall(r'<tspan[^>]*>(.*?)</tspan>', m.group(3)) or [m.group(3)]
                for line in lines:
                    self.assertLessEqual(float(m.group(1)) + len(line) * 0.6 * size, width, line)

    def test_a_long_subtitle_wraps_into_lines_that_fit_and_the_canvas_grows(self):
        data = [{"workload": "t", "library": "x", "vs": "y", "verdict": "win", "ratio": 0.5, "own": 1.0, "rival": 2.0}]
        one = P.svg_chart("t", "short", data, "light", P.fmt_time, "a")
        long = P.svg_chart("t", P.CHART_NOTE * 2, data, "light", P.fmt_time, "a")
        self.assertEqual(long.count("<tspan"), len(P.wrap_text(P.CHART_NOTE * 2, 12, 828)))
        self.assertGreater(long.count("<tspan"), 1)
        height = lambda s: int(re.search(r'height="(\d+)"', s).group(1))  # noqa: E731
        self.assertGreater(height(long), height(one))
        for line in P.wrap_text(P.CHART_NOTE * 2, 12, 828):
            self.assertLessEqual(P.est_width(line, 12), 828)

    def test_the_report_charts_are_wrapped_too(self):
        with tempfile.TemporaryDirectory() as d:
            P.render(synthetic(), Path(d))
            for f in Path(d).glob("charts/*.svg"):
                svg = f.read_text(encoding="utf-8")
                self.assertNotRegex(svg, r'font-size="12" fill="[^"]+">[^<]{140,}</text>', f.name)


class AltText(Hero):
    def test_alt_is_short_and_the_full_numbers_are_in_the_description(self):
        self.run_cmd()
        alt = alt_of(self.block(self.readme, "bench-hero"))
        self.assertLess(len(alt), 300)
        self.assertIn("table below", alt)
        self.assertNotRegex(alt, r"µs|ns\b|×  ")
        for theme in ("light", "dark"):
            svg = svg_of(self, theme)
            self.assertIn(f'aria-label="{alt}"', svg)
            desc = desc_of(svg)
            for needle in ("generic load 1.00× (level) mixed (ours 100 ns vs 100 ns", "dump "):
                self.assertIn(needle, desc)


class Headline(Hero):
    def result(self):
        # generic-load: 4× faster on the only workload; dump: 2× slower.
        r = real_world(synthetic(), "w1")
        r["cells"] = [cell("ours", "own", workload="w1", t=25.0), cell("rival", workload="w1", t=100.0),
                      cell("ours", "own", task="dump", workload="w1", t=200.0),
                      cell("rival", task="dump", workload="w1", t=100.0)]
        return r

    def test_a_win_reads_n_times_faster_and_a_loss_n_times_slower_in_table_chart_and_description(self):
        self.run_cmd(self.result())
        hero = self.block(self.readme, "bench-hero")
        self.assertIn("| 4.00× faster |", hero)
        self.assertIn("| 2.00× slower |", hero)
        for theme in ("light", "dark"):
            svg = svg_of(self, theme)
            self.assertIn(">4.00× faster  ▲ win<", svg)
            self.assertIn(">2.00× slower  ▼ loss<", svg)
            self.assertIn("generic load 4.00× faster win", desc_of(svg))
            self.assertIn("dump 2.00× slower loss", desc_of(svg))
        self.assertNotIn("0.25×", hero)

    def test_faster_bars_grow_to_the_right_and_slower_bars_to_the_left(self):
        self.run_cmd(self.result())
        svg = svg_of(self)
        rects = [(float(x), float(w)) for x, w in re.findall(r'<rect x="([\d.]+)" y="\d+" width="([\d.]+)" height="16"', svg)]
        centre = float(re.search(r'<line x1="([\d.]+)"[^>]*stroke-dasharray', svg).group(1))
        (fx, fw), (sx, sw) = rects
        self.assertEqual(fx, centre)
        self.assertEqual(round(sx + sw, 1), centre)

    def test_a_tie_is_called_level_not_faster(self):
        c = []
        for w in "abc":
            c += [cell("ours", "own", workload=w, t=97.0), cell("rival", workload=w, t=100.0)]
        self.run_cmd(probe_result(c))
        self.assertIn("level (1.03× faster)", self.block(self.readme, "bench-hero"))

    def test_factors_keep_three_figures(self):
        for n, text in ((2.0412, "2.04×"), (10.43, "10.4×"), (357.4, "357×"), (35754.0, "35,800×")):
            self.assertEqual(M.fmt_factor(n), text)

    def test_the_full_page_keeps_ours_over_theirs_with_its_legend(self):
        self.run_cmd(self.result())
        full = self.block(self.docs / "performance.md", "bench-full")
        self.assertIn("0.25× of `rival`", full)
        self.assertIn("ours ÷ the strongest competitor in that row, so below 1.0× is better for us", full)


class Legend(Hero):
    OLD = re.compile(r"† names (a cell|a competitor|one) that did less than the full task")
    NEW = ("A † marks a cell measured differently from the task as specified; "
           "the note beside each mark says how.")

    def test_hero_full_page_and_report_use_the_new_sentence(self):
        self.run_cmd()
        for text in (self.block(self.readme, "bench-hero"), self.block(self.docs / "performance.md", "bench-full")):
            self.assertIn(self.NEW, text)
            self.assertNotRegex(text, self.OLD)
        self.assertIn("† marks a cell measured differently from the task as specified", P.CHART_NOTE)
        self.assertNotRegex(P.CHART_NOTE, self.OLD)

    def test_our_own_marked_cell_carries_the_mark_and_its_note(self):
        r = real_world(synthetic(), "w1")
        r["cells"] = [cell("ours", "own", workload="w1", t=50.0, variant="bytes-only"), cell("rival", workload="w1", t=100.0)]
        self.run_cmd(r)
        row = next(l for l in self.block(self.readme, "bench-hero").split("\n") if l.startswith("| generic load"))
        self.assertIn("50 ns † (bytes-only)", row)


class Revision(Hero):
    def test_the_method_line_names_the_revision_that_wrote_it_and_no_kit_version(self):
        self.readme.write_bytes(f"{HERO}\n{END}\n".encode())
        for name in ("performance.md", "conformance.md"):
            (self.docs / name).unlink()
        self.assertEqual(self.run_cmd(conf=None), 0)
        method = next(l for l in self.block(self.readme, "bench-hero").split("\n") if l.startswith("Method:"))
        self.assertIn(f"text generated by kit revision {REVISION}", method)
        self.assertNotRegex(method, r"kit version|0\.\d+\.\d+")

    def git(self, *args):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        return subprocess.run(["git", *args], cwd=self.dir / "kit", check=True, capture_output=True, text=True, env=env).stdout

    def test_revision_of_a_checkout_marks_modified_files_and_ignores_untracked_ones(self):
        (self.dir / "kit").mkdir()
        self.git("init", "-q")
        (self.dir / "kit" / "f").write_text("a")
        self.git("add", "f")
        self.git("commit", "-q", "-m", "x")
        head = self.git("rev-parse", "--short=12", "HEAD").strip()
        with mock.patch.dict(os.environ, {"KIT_REVISION": ""}):
            root = self.dir / "kit"
            self.assertEqual(RENDERER_REVISION(root), head)
            (root / "new").write_text("untracked")
            self.assertEqual(RENDERER_REVISION(root), head)
            (root / "f").write_text("b")
            self.assertEqual(RENDERER_REVISION(root), head + "+modified")

    def test_a_tree_without_history_uses_the_launcher_revision(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"KIT_REVISION": "feedface0000"}):
            self.assertEqual(RENDERER_REVISION(Path(d)), "feedface0000")


class Golden(Hero):
    def test_matches_the_golden_hero(self):
        self.run_cmd(real_world(full_run(synthetic()), "w1", "w2"), smoke=False)
        text = self.block(self.readme, "bench-hero") + "\n" + desc_of(svg_of(self)) + "\n"
        if os.environ.get("UPDATE_GOLDEN"):
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(text, encoding="utf-8")
        self.assertEqual(text, GOLDEN.read_text(encoding="utf-8"),
                         "the hero changed; if that is intended, rerun with UPDATE_GOLDEN=1")


if __name__ == "__main__":
    unittest.main()
