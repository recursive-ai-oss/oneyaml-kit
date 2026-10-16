"""Self-tests for the reasons given to competitor cells without a number: every such cell is
explained, a cell of our own library never is, and a reason that matches nothing is reported."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from oneyaml_bench import cli, reasons  # noqa: E402
from oneyaml_bench import report as P  # noqa: E402
from oneyaml_kit import public_text  # noqa: E402
from test_readme import Base  # noqa: E402
from test_report import synthetic  # noqa: E402
from test_results import cell  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "fixtures" / "reason_samples.json"
ENTRY = {"libraries": ["rival"], "tasks": ["generic-load"], "detail": "got (\\S+), expected \\1\\.0",
         "reason": "Writes 1.0 as 1."}


def result_with(cells, mode="quick"):
    r = synthetic()
    r["cells"] = [{**c, "workload": "w1" if c["workload"] == "w" else c["workload"]} for c in cells]
    r["run"].update(mode=mode, smoke=mode == "quick")
    return r


class Entries(unittest.TestCase):
    def test_shipped_entries_are_well_formed_and_public(self):
        entries = reasons.load()
        self.assertTrue(entries)
        for e in entries:
            self.assertIsNone(public_text.PATTERN.search(e["reason"]), e["reason"])

    def test_every_entry_explains_a_real_first_difference(self):
        # The samples are first differences from real runs. An entry that explains none of them has
        # drifted from what libraries actually do, and a sample that no longer matches its entry has too.
        entries = reasons.load()
        samples = json.loads(SAMPLES.read_text(encoding="utf-8"))["samples"]
        self.assertEqual({s["entry"] for s in samples}, set(range(len(entries))))
        for s in samples:
            self.assertEqual(reasons.match_index(entries, s), s["entry"], s)

    def test_a_bad_entry_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ({**ENTRY, "detail": "("}, {**ENTRY, "reason": ""}, {**ENTRY, "libraries": []},
                        {**ENTRY, "extra": 1}, {**ENTRY, "status": ["unjudged"]},
                        {k: v for k, v in ENTRY.items() if k != "detail"}, {**ENTRY, "detail": ""},
                        {**ENTRY, "detail": "x*"}, {**ENTRY, "detail": "a|"}, {**ENTRY, "tasks": ["no-such-task"]}):
                p = Path(d) / "r.json"
                p.write_text(json.dumps({"entries": [bad]}), encoding="utf-8")
                with self.assertRaises(reasons.ReasonError, msg=bad):
                    reasons.load(p)


class Overbroad(unittest.TestCase):
    def explain(self, lib, task, status, detail):
        c = cell(lib, task=task, status=status, detail=detail)
        i = reasons.match_index(reasons.load(), c)
        return None if i is None else reasons.load()[i]["reason"]

    def test_errors_and_panics_get_no_layout_reason(self):
        for lib, task, status, detail in (
                ("yaml-v3", "document-parse", "rejects", "yaml: line 3: mapping values are not allowed in this context"),
                ("goccy-go-yaml", "edit-script", "rejects", "panic: runtime error: index out of range [3] with length 3"),
                ("palantir-yamlpatch", "patch", "rejects", "replace operation does not apply: doc is missing key: /x"),
                ("yaml2jsonnode", "patch", "rejects", "JsonException: unexpected end of data"),
                ("json-patch", "patch", "rejects", 'doc is missing path: "/spec/replicas": missing value'),
                ("yaml-v3", "unchanged-emit", "error", "the process died (exit status 2)")):
            self.assertIsNone(self.explain(lib, task, status, detail), (lib, detail))

    def test_an_output_difference_still_gets_its_reason(self):
        detail = 'byte 0: got 10 bytes "a: 1\\n", expected 20 bytes "# head\\na: 1\\n"'
        self.assertIn("blank lines", self.explain("yaml-v3", "unchanged-emit", "differs", detail))
        self.assertIsNone(self.explain("yaml-v3", "unchanged-emit", "rejects", detail))


class Rendering(unittest.TestCase):
    def render(self, result):
        with tempfile.TemporaryDirectory() as d:
            return P.render(result, Path(d))

    def test_a_competitor_cell_without_a_number_carries_a_note_with_its_first_difference(self):
        md = self.render(synthetic())
        w3 = next(l for l in md.splitlines() if l.startswith("| `w3`") and "differs" in l)
        self.assertIn("differs [1]", w3)
        notes = md.split("### Notes on competitor cells")[1]
        self.assertIn("1. `other` (differs; generic-load ×1)", notes)
        self.assertIn("`at /a/b: got 1, expected 2`", notes)

    def test_a_curated_reason_is_shown_beside_the_first_difference(self):
        cells = [cell("ours", "own", t=50.0), cell("rival", status="differs", detail="at /x: got 1, expected 1.0")]
        notes = reasons.Notes(cells, [ENTRY])
        line = notes.lines()[0]
        self.assertIn("Writes 1.0 as 1.", line)
        self.assertIn("at /x: got 1, expected 1.0", line)

    def test_a_reason_does_not_explain_a_difference_of_another_kind(self):
        cells = [cell("rival", status="differs", detail="at /x: got 2, expected 3"),
                 cell("rival", task="dump", status="differs", detail="at /x: got 1, expected 1.0"),
                 cell("other", status="differs", detail="at /x: got 1, expected 1.0"),
                 cell("rival", workload="u", status="unjudged", detail="at /x: got 1, expected 1.0")]
        for line in reasons.Notes(cells, [ENTRY]).lines():
            self.assertNotIn("Writes 1.0 as 1.", line)

    def test_a_cell_of_our_own_library_is_never_explained(self):
        # Even an entry that names our library cannot turn its failure into a note.
        own = {**ENTRY, "libraries": ["ours", "rival"]}
        cells = [cell("ours", "own", status="differs", detail="at /x: got 1, expected 1.0")]
        self.assertEqual(reasons.Notes(cells, [own]).groups, [])
        md = self.render(synthetic())
        row = next(l for l in md.splitlines() if l.startswith("| `w4`"))
        self.assertNotRegex(row, r"differs \[")
        own_line = next(l for l in md.splitlines() if l.startswith("| rust | `ours` | generic-load | `w4`"))
        self.assertTrue(own_line.endswith("| — |"), own_line)
        self.assertNotIn("`ours` (", md.split("### Notes on competitor cells")[1].split("## Workloads")[0])

    def test_every_competitor_cell_without_a_number_has_a_note(self):
        r = synthetic()
        notes = reasons.Notes(r["cells"])
        for c in r["cells"]:
            if c["role"] == "competitor" and c["status"] in ("differs", "rejects", "unjudged", "error"):
                self.assertTrue(notes.marker(c), c)
            else:
                self.assertEqual(notes.marker(c), "", c)


class ReadmeEvidence(Base):
    def test_the_evidence_page_explains_competitor_cells_and_not_ours(self):
        self.assertEqual(self.run_cmd(), 0)
        full = self.block(self.docs / "performance.md", "bench-full")
        self.assertIn("differs [1]", full)
        notes = full.split("what the correctness guard found:")[1]
        self.assertIn("1. `other` (differs; generic-load ×1)", notes)
        self.assertIn("`at /a/b: got 1, expected 2`", notes)
        self.assertNotIn("`ours` (", notes)
        self.assertNotRegex(next(l for l in full.split("\n") if l.startswith("| generic load | `w4`")), r"differs \[")


class Stale(unittest.TestCase):
    def full(self, cells):
        return result_with(cells, "full")

    def test_an_entry_no_cell_uses_is_stale_in_a_full_run(self):
        r = self.full([cell("ours", "own", t=50.0), cell("rival", t=60.0)])
        self.assertEqual(reasons.stale(r, [ENTRY]), [ENTRY])
        with tempfile.TemporaryDirectory() as d:
            md = self._render_with(r, [ENTRY], d)
        self.assertIn("### Stale reasons", md)
        self.assertIn("Writes 1.0 as 1.", md.split("### Stale reasons")[1])

    def _render_with(self, r, entries, d):
        saved = reasons.load
        reasons.load = lambda path=None: entries
        try:
            return P.render(r, Path(d))
        finally:
            reasons.load = saved

    def test_an_entry_that_is_used_is_not_stale(self):
        r = self.full([cell("rival", status="differs", detail="got 1, expected 1.0")])
        self.assertEqual(reasons.stale(r, [ENTRY]), [])

    def test_an_entry_for_a_library_the_run_did_not_exercise_is_not_stale(self):
        r = self.full([cell("other", t=60.0)])
        self.assertEqual(reasons.stale(r, [ENTRY]), [])

    def test_a_library_no_harness_lists_is_stale_in_a_whole_run(self):
        typo = {**ENTRY, "libraries": ["rivall"]}
        r = self.full([cell("ours", "own", t=50.0), cell("rival", t=60.0)])
        r["harnesses"]["rust"]["libraries"].append({"id": "rival"})
        self.assertEqual(reasons.stale(r, [typo], {"rust"}), [typo])
        # A run that left a language out proves nothing about a name.
        self.assertEqual(reasons.stale(r, [typo], {"rust", "go"}), [])
        self.assertEqual(reasons.stale(r, [typo]), [])  # the default is every language of the kit

    def test_a_quick_run_is_not_judged(self):
        r = result_with([cell("rival", t=60.0)], "quick")
        self.assertEqual(reasons.stale(r, [ENTRY]), [])

    def test_an_entry_shadowed_by_an_earlier_one_is_stale(self):
        r = self.full([cell("rival", status="differs", detail="got 1, expected 1.0")])
        twin = {**ENTRY, "reason": "Another."}
        self.assertEqual(reasons.stale(r, [ENTRY, twin]), [twin])

    def test_the_command_fails_on_a_stale_entry(self):
        r = self.full([cell("ours", "own", t=50.0), cell("rival", t=60.0)])
        saved = reasons.load
        reasons.load = lambda path=None: [ENTRY]
        try:
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "results.json"
                p.write_text(json.dumps(r), encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(["report", str(p)]), 1)
        finally:
            reasons.load = saved


if __name__ == "__main__":
    unittest.main()
