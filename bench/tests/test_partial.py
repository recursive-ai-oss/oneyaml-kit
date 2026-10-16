"""A run shows its progress, keeps every language that finished, and says when a language is missing.

The harnesses here are a tiny script that speaks the harness protocol, so no toolchain is needed.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from oneyaml_bench import cli, readme, report, results, run  # noqa: E402

FAKE = r'''
import json, sys
a = sys.argv[1:]
out = a[a.index("--out") + 1]
lang = sys.argv[0].split("-")[-1].removesuffix(".py")
if "fail" in lang:
    sys.exit(3)
rec = {"type": "harness", "name": "fake", "version": "1", "runtime": "r", "target": "t", "tool": "tool",
       "timer": "t", "memory": "m",
       "libraries": [{"id": "own", "package": "own", "version": "1", "role": "own", "settings": {}},
                     {"id": "rival", "package": "rival", "version": "1", "role": "competitor", "settings": {}}]}
with open(out, "w") as f:
    print(json.dumps(rec), file=f, flush=True)
    for lib in ("own", "rival"):
        print(json.dumps({"type": "begin", "cell": f"{lib}/dump/w"}), file=f, flush=True)
        print(json.dumps({"type": "cell", "library": lib, "task": "dump", "workload": "w", "status": "ok",
                          "input_bytes": 1, "time": {"ns": 100.0, "stat": "mean"},
                          "memory": {"allocated_bytes": 10, "allocations": 1}}), file=f, flush=True)
'''

META = {"id": "w", "kind": "generated", "tier": "quick", "description": "d", "source": {}, "documents": 1,
        "input_bytes": 1, "input_sha256": "0", "tasks": ["dump"], "notes": ""}


def harness(tmp: Path, lang: str) -> dict:
    script = tmp / f"fake-{lang}.py"
    script.write_text(FAKE, encoding="utf-8")
    return {"language": lang, "_dir": tmp, "build": [], "command": [sys.executable, str(script)]}


class PartialRuns(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.tmp = Path(self.t.name)
        self.addCleanup(self.t.cleanup)
        self.lines = []

    def go(self, langs, select=None):
        found = {l: harness(self.tmp, l) for l in langs}
        with mock.patch.object(run, "discover", return_value=found), \
                mock.patch.object(run, "build"), \
                mock.patch.object(run.workloads, "prepare", return_value=[META]):
            return run.run("full", self.tmp / "out", select, log=self.lines.append, build_dir=self.tmp)

    def test_one_progress_line_per_cell_and_a_line_per_language(self):
        self.go(["aa"])
        cells = [l for l in self.lines if l.startswith("  aa ")]
        self.assertEqual(len(cells), 2)
        self.assertIn("aa own dump w: ok", cells[0])
        self.assertIn("aa rival dump w: ok", cells[1])
        self.assertTrue(any(l.startswith("start aa") for l in self.lines))
        self.assertTrue(any(l.startswith("end aa: 2 cells") for l in self.lines))

    def test_each_language_is_written_when_it_finishes_so_a_cut_short_run_keeps_it(self):
        out = self.tmp / "out"
        with self.assertRaises(run.HarnessError):
            self.go(["aa", "zz-fail"])
        self.assertTrue((out / "results-aa.json").is_file())
        self.assertFalse((out / "results-zz-fail.json").exists())
        self.assertFalse((out / "results.json").exists())
        got = results.load(out)
        results.validate(got)
        self.assertEqual(sorted(got["harnesses"]), ["aa"])
        self.assertEqual(results.missing_languages(got), ["zz-fail"])
        with tempfile.TemporaryDirectory() as t:
            md = report.render(got, Path(t))
        self.assertIn("INCOMPLETE RUN", md)
        self.assertIn("zz-fail", md)
        self.assertEqual(cli.text_hits(md), [])

    def test_a_whole_run_is_complete_and_a_limited_run_is_not(self):
        whole = self.go(["aa", "bb"])
        self.assertEqual(results.missing_languages(whole), [])
        with tempfile.TemporaryDirectory() as t:
            self.assertNotIn("INCOMPLETE", report.render(whole, Path(t)))

    def test_a_run_limited_to_a_language_is_complete_when_that_language_finishes(self):
        part = self.go(["aa", "bb"], ["bb"])
        self.assertEqual(part["run"]["expected_languages"], ["bb"])
        self.assertEqual(results.missing_languages(part), [])
        with tempfile.TemporaryDirectory() as t:
            self.assertNotIn("INCOMPLETE", report.render(part, Path(t)))
        ctx = mock.Mock(result=part, run=part["run"])
        self.assertFalse(any("INCOMPLETE" in l for l in readme.smoke_notice(ctx)))

    def test_a_limited_run_cut_short_before_its_language_finishes_is_incomplete(self):
        out = self.tmp / "out"
        with self.assertRaises(run.HarnessError):
            self.go(["aa", "zz-fail"], ["aa", "zz-fail"])
        got = results.load(out)
        self.assertEqual(results.missing_languages(got), ["zz-fail"])
        with tempfile.TemporaryDirectory() as t:
            self.assertIn("INCOMPLETE RUN", report.render(got, Path(t)))

    def test_merging_per_language_files_equals_the_whole_result(self):
        whole = self.go(["aa", "bb"])
        out = self.tmp / "out"
        merged = results.merge([json.loads((out / f"results-{l}.json").read_text()) for l in ("aa", "bb")])
        self.assertEqual(merged["cells"], whole["cells"])
        self.assertEqual(sorted(merged["harnesses"]), ["aa", "bb"])
        self.assertEqual(results.missing_languages(merged), [])

    def test_an_earlier_runs_files_do_not_pass_for_this_one(self):
        out = self.tmp / "out"
        out.mkdir()
        (out / "results-old.json").write_text("{}", encoding="utf-8")
        (out / "results.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(run.HarnessError):
            self.go(["zz-fail"])
        self.assertIsNone(results.load(out))

    def test_a_readme_block_from_an_incomplete_run_says_so(self):
        with self.assertRaises(run.HarnessError):
            self.go(["aa", "zz-fail"])
        part = results.load(self.tmp / "out")
        ctx = mock.Mock(result=part, run=part["run"])
        self.assertTrue(any("INCOMPLETE RUN" in l and "zz-fail" in l for l in readme.smoke_notice(ctx)))


if __name__ == "__main__":
    unittest.main()
