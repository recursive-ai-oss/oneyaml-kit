"""Self-tests for the generated README and docs blocks: marker handling, what the tables must
show, and that re-running changes nothing."""

import contextlib
import copy
import io
import json
import math
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from oneyaml_bench import cli  # noqa: E402
from oneyaml_bench import readme as M  # noqa: E402
from oneyaml_bench import report as P  # noqa: E402
from test_report import synthetic  # noqa: E402
from test_results import cell  # noqa: E402

END = M.END_MARK
HERO = "<!-- generated:bench-hero -->"
LINE = "<!-- generated:conformance-line -->"
FULL = "<!-- generated:bench-full -->"
DEV = "<!-- generated:conformance-deviations -->"
README = f"# Title\r\nintro  \n\n{HERO}\nold hero\n{END}\n\nmiddle \t\n\n{LINE}\nold line\n{END}\nlast\n"
PERF = f"# Performance\n\n{FULL}\nold\n{END}\n\ntail  \n"
CONF = f"# Conformance\n\n{DEV}\nold\n{END}\n"

CONFORMANCE = {
    "adapter": {"name": "ours"},
    "kit": {"version": "0.9.0", "suites": {"yaml-test-suite": {"release": "data-2022-01-17"}, "own": {"cases": 2}}},
    "run": {"complete": True},
    "cases": [{"suite": "yaml-test-suite", "loose": "pass", "strict": "pass"},
              {"suite": "yaml-test-suite", "loose": "pass", "strict": "fail",
               "corrected": {"loose": "pass", "strict": "pass"}},
              {"suite": "own", "loose": "pass", "strict": "fail"}],
    "deviations": {"ok": True, "entries": [{"case": "yts:XXXX", "expectation": "primary", "mode": "strict",
                                "verdict": "fail", "reason": "reads | as text"}]},
}


def real_world(result: dict, *ids: str) -> dict:
    for w in result["workloads"]:
        w["kind"] = "real-world" if w["id"] in ids else "generated"
    return result


def full_run(result: dict) -> dict:
    result = copy.deepcopy(result)
    result["run"].update(mode="full", smoke=False)
    return result


class Base(unittest.TestCase):
    def setUp(self):
        # The vendored suite has hundreds of cases; the fixtures have two.
        p = mock.patch.object(M, "pinned_yts", return_value=("data-2022-01-17", 2))
        p.start()
        self.addCleanup(p.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.readme = self.dir / "README.md"
        self.docs = self.dir / "docs"
        self.docs.mkdir()
        self.readme.write_bytes(README.encode())
        (self.docs / "performance.md").write_bytes(PERF.encode())
        (self.docs / "conformance.md").write_bytes(CONF.encode())

    def put(self, result=None, conf=CONFORMANCE):
        res = self.dir / "res"
        (res / "bench").mkdir(parents=True, exist_ok=True)
        (res / "bench" / "results.json").write_text(json.dumps(result or real_world(synthetic(), "w1", "w2")))
        (res / "conformance").mkdir(exist_ok=True)
        if conf is not None:
            (res / "conformance" / "ours.json").write_text(json.dumps(conf))
        return res

    def run_cmd(self, result=None, smoke=True, conf=CONFORMANCE, library="ours", extra=()):
        res = self.put(result, conf)
        args = ["readme", "--results", str(res), "--library", library, "--readme", str(self.readme),
                "--docs", str(self.docs), *extra]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(args + (["--allow-smoke"] if smoke else []))

    def files(self):
        return {p: p.read_bytes() for p in [self.readme, *self.docs.glob("*.md"), *self.dir.glob("docs/img/*")]}

    def block(self, path, kind) -> str:
        t = path.read_bytes().decode()
        k, a, b = next(x for x in M.scan_blocks(t, set(M.KINDS)) if x[0] == kind)
        return t[a:b]

    def hero_line(self, task):
        return next(l for l in self.block(self.readme, "bench-hero").split("\n") if l.startswith(f"| {task} "))


class Splice(Base):
    def test_only_marked_regions_change_and_rerun_is_identical(self):
        self.assertEqual(self.run_cmd(), 0)
        first = self.files()
        old = README.encode()
        new = self.readme.read_bytes()
        self.assertTrue(new.startswith(old[:old.index(HERO.encode()) + len(HERO)]))
        self.assertTrue(new.endswith(old[old.index(b"last"):]))
        self.assertIn(b"\n\nmiddle \t\n\n" + LINE.encode(), new)
        self.assertNotIn(b"old hero", new)
        self.assertNotIn(b"old line", new)
        self.assertTrue((self.docs / "performance.md").read_bytes().endswith(b"\ntail  \n"))
        self.assertEqual(self.run_cmd(), 0)
        self.assertEqual(first, self.files())

    def test_several_blocks_in_a_file_and_blocks_in_each_file(self):
        self.run_cmd()
        self.assertIn("yaml-test-suite", self.block(self.readme, "conformance-line"))
        self.assertIn("Workload", self.block(self.docs / "performance.md", "bench-full"))
        self.assertIn("yts:XXXX", self.block(self.docs / "conformance.md", "conformance-deviations"))
        self.assertIn("Strongest competitor", self.block(self.readme, "bench-hero"))

    def test_crlf_marker_keeps_crlf(self):
        self.readme.write_bytes(f"a\r\n{HERO}\r\nold\r\n{END}\r\nz\r\n".encode())
        self.run_cmd()
        data = self.readme.read_bytes()
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_marker_errors_leave_every_file_untouched(self):
        cases = {
            "unknown": (f"<!-- generated:nope -->\n{END}\n", "unknown block kind"),
            "none": ("text\n", "no generated markers"),
            "missing end": (f"{HERO}\n", "missing end"),
            "extra end": (f"{END}\n{HERO}\n{END}\n", "no begin"),
            "duplicated": (f"{HERO}\n{END}\n{HERO}\n{END}\n", "duplicated"),
            "nested": (f"{HERO}\n{LINE}\n{END}\n{END}\n", "nested"),
        }
        for name, (text, msg) in cases.items():
            with self.subTest(name):
                self.readme.write_bytes(text.encode())
                before = self.files()
                with self.assertRaises(M.ReadmeError) as cm:
                    M.scan_blocks(text, set(M.KINDS), "README") if name != "none" else M.update(
                        M.Ctx(full_run(real_world(synthetic(), "w1")), "ours", "0", None, self.dir, False),
                        [self.readme], "c", self.dir)
                self.assertIn(msg, str(cm.exception))
                self.assertEqual(self.run_cmd(), 1)
                self.assertEqual(before, self.files())

    def test_a_bad_docs_file_stops_the_readme_from_changing(self):
        (self.docs / "performance.md").write_bytes(f"{FULL}\nx\n".encode())
        before = self.files()
        self.assertEqual(self.run_cmd(), 1)
        self.assertEqual(before, self.files())
        self.assertFalse((self.dir / "docs" / "img").exists())

    def test_marker_for_a_block_without_its_input_is_an_error(self):
        before = self.files()
        self.assertEqual(self.run_cmd(conf=None), 1)
        self.assertEqual(before, self.files())


class Hero(Base):
    def test_geometric_mean_over_real_world_workloads(self):
        # generic-load: w1 is 0.5x, w2 is 2.0x; the geometric mean is 1.0x, a tie. w3 and w4 are generated.
        self.run_cmd()
        line = self.hero_line("generic load")
        self.assertIn("1.00× (level)", line)
        self.assertIn("◆ mixed", line)
        self.assertNotIn("▲ win", line)
        self.assertIn("`rival 2.0`", line)
        self.assertIn("real-world workloads", self.block(self.readme, "bench-hero"))
        r = real_world(synthetic(), "w1", "w3")  # 0.5 and 0.8
        self.run_cmd(r)
        self.assertIn(f"{1 / math.sqrt(0.5 * 0.8):.2f}× faster", self.hero_line("generic load"))
        self.assertIn("▲ win", self.hero_line("generic load"))

    def test_a_loss_is_a_loss(self):
        self.run_cmd(real_world(synthetic(), "w2"))
        line = self.hero_line("generic load")
        self.assertIn("2.00× slower", line)
        self.assertIn("▼ loss", line)

    def test_not_comparable_workload_is_not_dropped(self):
        self.run_cmd(real_world(synthetic(), "w1", "w4"))  # ours differs on w4
        line = self.hero_line("generic load")
        self.assertIn("`w4` (differs)", self.block(self.readme, "bench-hero"))
        self.assertIn("partial", line)
        r = real_world(synthetic(), "w4")  # only the failing workload
        self.run_cmd(r)
        line = self.hero_line("generic load")
        self.assertIn("NOT COMPARABLE", line)
        self.assertNotIn("×", line)

    def test_tasks_without_cells_are_left_out_and_unsupported_is_not_a_number(self):
        self.run_cmd()
        hero = self.block(self.readme, "bench-hero")
        self.assertNotIn("typed load", hero)
        self.assertIn("NOT COMPARABLE", self.hero_line("patch"))
        self.assertIn("(unsupported)", hero)

    def test_task_without_a_real_world_workload_names_its_basis(self):
        self.run_cmd(real_world(synthetic()))
        self.assertIn("all workloads for", self.block(self.readme, "bench-hero"))

    def test_marked_competitor_is_named_and_counts(self):
        r = real_world(synthetic(), "w2")
        r["cells"] = [c for c in r["cells"] if c["workload"] != "w2"] + [
            cell("ours", "own", workload="w2", t=200.0),
            cell("rival", workload="w2", t=100.0, variant="bytes-only")]
        self.run_cmd(r)
        line = self.hero_line("generic load")
        self.assertIn("†", line)
        self.assertIn("▼ loss", line)

    def test_smoke_is_labelled_and_refused_without_the_flag(self):
        before = self.files()
        self.assertEqual(self.run_cmd(smoke=False), 1)
        self.assertEqual(before, self.files())
        self.run_cmd()
        for path, kind in ((self.readme, "bench-hero"), (self.docs / "performance.md", "bench-full")):
            self.assertIn("SMOKE RESULT", self.block(path, kind))
        method = next(l for l in self.block(self.readme, "bench-hero").splitlines() if l.startswith("Method:"))
        self.assertEqual(method.count("run of"), 1, method)

    def test_publishable_run_is_labelled_full_run(self):
        self.assertEqual(self.run_cmd(full_run(real_world(synthetic(), "w1", "w2")), smoke=False), 0)
        hero = self.block(self.readme, "bench-hero")
        self.assertIn("**full run**", hero)
        self.assertNotIn("SMOKE RESULT", hero)
        # The mode is written once in the method line, not repeated as a label.
        method = next(l for l in hero.splitlines() if l.startswith("Method:"))
        self.assertEqual(method.count("full run"), 1, method)
        self.assertTrue(method.startswith("Method: **full run** of 2026-01-01"), method)

    def test_method_strip(self):
        self.run_cmd()
        hero = self.block(self.readme, "bench-hero")
        for needle in ("2026-01-01", "Kit 0.9.0", "revision abc", "`ours 1.0`", "`rival 2.0`", "`other 3.0`",
                       "rustc x", "criterion", "Test CPU", "Linux", "0.5 before"):
            self.assertIn(needle, hero)

    def test_chart_embedded_with_numbers_in_alt(self):
        self.run_cmd()
        hero = self.block(self.readme, "bench-hero")
        self.assertIn('srcset="docs/img/ours-hero-dark.svg"', hero)
        self.assertIn('src="docs/img/ours-hero-light.svg"', hero)
        for theme in ("light", "dark"):
            self.assertIn("<svg", (self.dir / "docs" / "img" / f"ours-hero-{theme}.svg").read_text())
        # The table follows the chart and holds the numbers, so the alt text is short and the image's own
        # description carries them.
        alt = hero[hero.index('alt="') + 5:]
        alt = alt[:alt.index('"')]
        self.assertLess(len(alt), 300)
        self.assertNotIn("generic load", alt)
        for theme in ("light", "dark"):
            svg = (self.dir / "docs" / "img" / f"ours-hero-{theme}.svg").read_text()
            desc = svg[svg.index("<desc>"):svg.index("</desc>")]
            self.assertIn("generic load 1.00× (level)", desc)
            self.assertIn("dump 1.11× faster", desc)

    def test_chart_path_is_relative_to_each_file(self):
        (self.docs / "performance.md").write_bytes(f"{HERO}\n{END}\n".encode())
        self.run_cmd()
        self.assertIn('srcset="img/ours-hero-dark.svg"', self.block(self.docs / "performance.md", "bench-hero"))


class Other(Base):
    def test_full_table_has_every_workload_and_every_competitor(self):
        self.run_cmd()
        full = self.block(self.docs / "performance.md", "bench-full")
        head = next(l for l in full.split("\n") if l.startswith("| Task"))
        for lib in ("ours", "rival", "other"):
            self.assertIn(f"`{lib}`", head)
        rows = [l for l in full.split("\n") if l.startswith("| generic load")]
        self.assertEqual(len(rows), 8)  # four workloads, time and memory
        w4 = next(l for l in rows if "`w4`" in l)
        self.assertIn("no number (differs)", w4)
        self.assertIn("▼ loss", next(l for l in rows if "`w2`" in l))

    def test_conformance_line(self):
        self.run_cmd()
        line = self.block(self.readme, "conformance-line")
        self.assertIn("yaml-test-suite data-2022-01-17: 2/2 loose, 1/2 strict.", line)
        self.assertIn("the fast set: 1 of 2, the slow ones were not run: 1/1 loose, 0/1 strict", line)
        self.assertIn("corrected expectations for 1 of them: 1/1 loose, 1/1 strict", line)
        self.assertIn("(docs/conformance.md)", line)

    def test_conformance_of_another_library_is_refused(self):
        before = self.files()
        self.assertEqual(self.run_cmd(conf={**CONFORMANCE, "adapter": {"name": "other"}}), 1)
        self.assertEqual(before, self.files())

    def test_deviations_listed_with_reasons(self):
        self.run_cmd()
        dev = self.block(self.docs / "conformance.md", "conformance-deviations")
        self.assertIn("`yts:XXXX`", dev)
        self.assertIn("reads \\| as text", dev)

    def test_unknown_library_is_an_error(self):
        before = self.files()
        self.assertEqual(self.run_cmd(library="nope"), 1)
        self.assertEqual(before, self.files())

    def test_output_passes_the_public_text_check(self):
        self.run_cmd()
        self.assertEqual(cli.public_text_hits([p for p in self.files() if p.suffix in (".md", ".svg")]), [])
        self.assertEqual(cli.text_hits("see the al" + "era plan"), ["1: 'al" + "era'"])
        bad = {**CONFORMANCE, "deviations": {"entries": [{**CONFORMANCE["deviations"]["entries"][0],
                                                          "reason": "ask the arch" + "itect"}]}}
        before = self.files()
        self.assertEqual(self.run_cmd(conf=bad), 1)
        self.assertEqual(before, self.files())



def probe_result(cells):
    r = synthetic()
    r["cells"] = cells
    r["workloads"] = [{"id": w, "kind": "real-world", "tier": "quick", "description": "d", "documents": 1,
                       "input_bytes": 1000, "tasks": ["generic-load"],
                       "source": {"origin": "g", "generator": "g", "licence": "CC0-1.0"}}
                      for w in sorted({c["workload"] for c in cells})]
    return r


class Marks(Base):
    """A row is never marked better than its workloads allow, for time and for memory, and the chart
    and its alt text carry the same marks and every task."""

    def alt(self):
        hero = self.block(self.readme, "bench-hero")
        alt = hero[hero.index('alt="') + 5:]
        return alt[:alt.index('"')]

    def test_all_workloads_level_is_not_a_win(self):
        c = []
        for w in "abc":
            c += [cell("ours", "own", workload=w, t=97.0), cell("rival", workload=w, t=100.0)]
        self.run_cmd(probe_result(c))
        line = self.hero_line("generic load")
        self.assertIn("● tie", line)
        self.assertNotIn("▲ win", line)
        self.assertIn("level (", line)

    def test_a_losing_workload_makes_the_row_mixed_in_table_chart_and_alt(self):
        c = [cell("ours", "own", workload="a", t=190.0), cell("rival", workload="a", t=100.0),
             cell("ours", "own", workload="b", t=60.0), cell("rival", workload="b", t=100.0),
             cell("ours", "own", workload="c", t=80.0), cell("rival", workload="c", t=100.0)]
        self.run_cmd(probe_result(c))
        line = self.hero_line("generic load")
        self.assertIn("◆ mixed", line)
        self.assertNotIn("▲ win", line)
        alt = self.alt()
        self.assertIn("1 tasks: 0 better, 0 level, 1 mixed, 0 worse", alt)
        self.assertNotIn("1 workloads", alt)
        self.assertIn("mixed", alt)
        svg = (self.dir / "docs" / "img" / "ours-hero-light.svg").read_text()
        self.assertIn(f'fill="{P.THEMES["light"]["mixed"]}"', svg)
        self.assertNotIn(f'fill="{P.THEMES["light"]["win"]}"', svg)
        self.assertIn("◆ mixed", svg)

    def memory_cells(self):
        c = []
        for w, op, rp, xp in (("a", 1000, 4000, 3000), ("b", 1000, 4000, 3000), ("c", 3000, 4000, 1000)):
            c += [cell("ours", "own", workload=w, t=90.0, peak=op), cell("rival", workload=w, t=100.0, peak=rp),
                  cell("other", workload=w, t=200.0, peak=xp)]
        return c

    def test_memory_mark_counts_workloads_and_names_the_memory_competitor(self):
        self.run_cmd(probe_result(self.memory_cells()))
        line = self.hero_line("generic load")
        self.assertIn("`rival 2.0`", line.split("|")[3])  # the time winner
        # Memory is not in the hero; the full page names the memory competitor of each workload.
        self.assertNotIn("allocated", self.block(self.readme, "bench-hero").split("Method:")[0])
        full = self.block(self.docs / "performance.md", "bench-full")
        row = next(l for l in full.split("\n") if l.startswith("| generic load | `c` |") and "of `other`" in l)
        self.assertIn("▼ loss", row)

    def p4_result(self):
        return probe_result([
            cell("ours", "own", workload="a", t=50.0), cell("rival", workload="a", t=100.0),
            cell("ours", "own", workload="b", status="differs", detail="x"), cell("rival", workload="b", t=100.0),
            cell("ours", "own", task="dump", workload="a", status="rejects", detail="x"),
            cell("rival", task="dump", workload="a", t=100.0)])

    def test_every_task_is_in_the_chart_and_the_alt_text(self):
        self.run_cmd(self.p4_result())
        alt = self.alt()
        self.assertIn("2 tasks:", alt)
        self.assertIn("1 not comparable", alt)
        for theme in ("light", "dark"):
            svg = (self.dir / "docs" / "img" / f"ours-hero-{theme}.svg").read_text()
            desc = svg[svg.index("<desc>"):svg.index("</desc>")]
            self.assertIn("dump NOT COMPARABLE (a rejects)", desc)
            self.assertIn("partial, no number for b (differs)", desc)
            self.assertIn("dump", svg)
            self.assertIn("NOT COMPARABLE (a rejects)", svg)
            self.assertIn("partial: b differs", svg)


class Conformance(Base):
    def refused(self, conf):
        before = self.files()
        self.assertEqual(self.run_cmd(conf=conf), 1)
        self.assertEqual(before, self.files())

    def test_a_filtered_run_is_refused(self):
        self.refused({**CONFORMANCE, "run": {"complete": False}})

    def test_unlisted_or_stale_deviations_are_refused(self):
        self.refused({**CONFORMANCE, "deviations": {**CONFORMANCE["deviations"], "ok": False}})

    def test_a_partial_suite_is_refused(self):
        self.refused({**CONFORMANCE, "cases": CONFORMANCE["cases"][1:]})

    def test_another_release_is_refused(self):
        kit = {**CONFORMANCE["kit"], "suites": {**CONFORMANCE["kit"]["suites"],
                                                "yaml-test-suite": {"release": "main"}}}
        self.refused({**CONFORMANCE, "kit": kit})

    def test_the_deviations_block_alone_is_refused_too(self):
        (self.docs / "performance.md").unlink()
        self.readme.write_bytes(f"{HERO}\n{END}\n".encode())
        (self.docs / "conformance.md").write_bytes(CONF.encode())
        self.refused({**CONFORMANCE, "run": {"complete": False}})

    def test_the_real_pin_is_checked_by_default(self):
        mock.patch.stopall()  # the fixture has two cases; the vendored suite has hundreds
        before = self.files()
        self.assertEqual(self.run_cmd(), 1)
        self.assertEqual(before, self.files())

    def test_slow_own_cases_are_named(self):
        self.run_cmd()
        self.assertIn("the slow ones were not run", self.block(self.readme, "conformance-line"))
        kit = {**CONFORMANCE["kit"], "suites": {**CONFORMANCE["kit"]["suites"], "own": {"cases": 1}}}
        self.run_cmd(conf={**CONFORMANCE, "kit": kit})
        self.assertIn("all of them including the slow ones", self.block(self.readme, "conformance-line"))


class Details(Base):
    def test_no_merge_patch_task_until_a_harness_has_one(self):
        self.assertFalse([t for t in M.TASK_LABEL if "merge" in t])

    def test_rows_with_no_cell_of_ours_are_shown_as_not_run(self):
        r = real_world(synthetic(), "w1", "w2")
        r["cells"].append(cell("rival", task="document-parse", workload="w1", t=10.0))
        r["cells"].append(cell("rival", workload="w3", task="dump", t=10.0))
        self.run_cmd(r)
        hero = self.block(self.readme, "bench-hero")
        self.assertIn("document parse `w1`", hero)
        self.assertIn("dump `w3`", hero)
        self.assertTrue(any(l.startswith("| document parse") and "NOT RUN" in l for l in hero.split("\n")))
        self.assertIn("Not run by", self.block(self.docs / "performance.md", "bench-full"))

    def test_method_strip_discloses_how_it_was_measured_and_where_the_kit_version_comes_from(self):
        self.run_cmd()
        hero = self.block(self.readme, "bench-hero")
        for needle in ("Kit 0.9.0", "measured with criterion", "Memory: mm"):
            self.assertIn(needle, hero)
        self.readme.write_bytes(f"{HERO}\n{END}\n".encode())
        for name in ("performance.md", "conformance.md"):
            (self.docs / name).unlink()
        (self.dir / "res" / "conformance" / "ours.json").unlink()
        self.assertEqual(self.run_cmd(conf=None), 0)
        method = self.block(self.readme, "bench-hero")
        self.assertIn("text generated by kit revision", method)
        self.assertNotIn("kit version", method)

    def test_competitor_time_column_says_what_it_is(self):
        self.run_cmd()
        self.assertIn("Its time (geometric mean)", self.block(self.readme, "bench-hero"))

    def test_a_failed_write_leaves_every_file_whole(self):
        before = self.files()
        real = M.os.replace
        calls = []

        def flaky(a, b):
            calls.append(b)
            if len(calls) == 2:
                raise OSError("disk full")
            real(a, b)
        with mock.patch.object(M.os, "replace", flaky):
            self.assertEqual(self.run_cmd(), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual([p for p in self.dir.rglob("*.new")], [])
        for p, data in self.files().items():
            self.assertTrue(data == before.get(p, data) or b"generated" in data or b"<svg" in data)


if __name__ == "__main__":
    unittest.main()
