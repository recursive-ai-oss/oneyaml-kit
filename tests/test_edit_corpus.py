"""The edit corpus and the specification-coverage report."""

import json
import unittest

import support
from oneyaml_kit import cases as C
from oneyaml_kit import coverage

ALL = C.load_all(include_slow=True)
OWN = [c for c in ALL if c.suite == "own"]
EDIT = [c for c in OWN if c.category == "edit"]


def family(c):
    return c.id.split("/")[1].split("-")[0]


class Corpus(unittest.TestCase):
    def test_every_edit_case_is_a_patch(self):
        self.assertTrue((support.ROOT / "suites" / "own" / "cases" / "edit").is_dir())
        self.assertGreaterEqual(len(EDIT), 250)
        for c in EDIT:
            self.assertEqual(c.op, "patch", c.id)

    def test_every_sub_area_has_cases(self):
        n = {}
        for c in EDIT:
            n[family(c)] = n.get(family(c), 0) + 1
        for fam, least in {"set": 40, "keep": 40, "add": 30, "remove": 25, "move": 5, "copy": 5,
                           "test": 10, "pointer": 20, "alias": 10, "atomic": 5, "merge": 10}.items():
            self.assertGreaterEqual(n.get(fam, 0), least, fam)

    def test_every_operation_and_both_patch_formats_are_used(self):
        ops, fmts = set(), set()
        for c in EDIT:
            fmts.add(c.args.get("format", "json-patch"))
            if c.args.get("format", "json-patch") == "json-patch":
                ops |= {o["op"] for o in c.args["patch"]}
        self.assertTrue({"add", "remove", "replace", "move", "copy", "test"} <= ops)
        self.assertEqual(fmts, {"json-patch", "merge-patch"})

    def test_refusals_state_that_the_document_is_unchanged(self):
        errs = [c for c in EDIT if c.expect["kind"] == "error"]
        self.assertGreaterEqual(len(errs), 50)
        for c in errs:
            self.assertTrue(c.expect["unchanged"], c.id)
            self.assertNotEqual(c.expect["category"], "any", c.id)

    def test_every_error_category_of_a_patch_is_exercised(self):
        used = {c.expect["category"] for c in EDIT if c.expect["kind"] == "error"}
        self.assertTrue({"patch-path", "patch-test", "patch-invalid", "refused-alias",
                         "refused-ambiguous"} <= used)

    def test_cases_resting_on_a_convention_say_so(self):
        tagged = [c for c in EDIT if {"judgement", "comment-ownership"} & set(c.tags)]
        self.assertGreaterEqual(len(tagged), 8)
        self.assertLess(len(tagged), len(EDIT) // 8, "such cases stay few")
        for c in tagged:
            where = support.ROOT / "suites" / "own" / "cases" / c.id.split(":", 1)[1]
            meta = json.loads((where / "case.json").read_bytes())
            self.assertGreater(len(meta.get("notes", "")), 40, c.id)

    def test_edits_change_something(self):
        for c in EDIT:
            if c.expect["kind"] == "bytes" and c.args["patch"]:
                if c.args.get("format", "json-patch") == "merge-patch":
                    continue
                if all(o["op"] == "test" for o in c.args["patch"]):
                    continue
                self.assertNotEqual(c.expect["bytes"], c.input, c.id)

    def test_encodings_and_line_breaks_are_exercised(self):
        data = [c.input for c in EDIT]
        self.assertTrue(any(d.startswith(b"\xef\xbb\xbf") for d in data))
        self.assertTrue(any(d.startswith(b"\xff\xfe") or d.startswith(b"\xfe\xff") for d in data))
        self.assertTrue(any(b"\r\n" in d for d in data))
        self.assertTrue(any(b"\r" in d.replace(b"\r\n", b"") for d in data))

    def test_seeded_cases_name_their_source(self):
        seeded = [c for c in EDIT if c.provenance.get("source", "").endswith("oneyaml-rust")]
        self.assertGreaterEqual(len(seeded), 6)
        for c in seeded:
            self.assertEqual(c.provenance["revision"], "93e9e30", c.id)
            self.assertTrue(c.provenance.get("reference"), c.id)


class Coverage(unittest.TestCase):
    def test_every_named_production_exists(self):
        prods = coverage.production_numbers(OWN)
        self.assertTrue(all(1 <= p <= coverage.TOTAL for p in prods))
        self.assertGreaterEqual(len(prods), 50)

    def test_an_unknown_production_is_rejected(self):
        c = OWN[0]
        for bad_value in ("212", "x", "0"):
            bad = C.Case(**{**c.__dict__, "productions": [bad_value]})
            with self.assertRaises(ValueError):
                coverage.production_numbers([bad])

    def test_chapters_tile_the_productions(self):
        spans = list(coverage.CHAPTERS.values())
        self.assertEqual(spans[0][0], 1)
        self.assertEqual(spans[-1][1], coverage.TOTAL)
        for (a, b), (c, d) in zip(spans, spans[1:]):
            self.assertEqual(b + 1, c)

    def test_the_report_counts_what_the_cases_say(self):
        md = coverage.render(ALL)
        n = len(coverage.production_numbers(OWN))
        self.assertIn(f"**{n} of {coverage.TOTAL}**", md)
        self.assertIn(f"Own cases: {len(OWN)}", md)
        for cat in coverage.CATEGORIES:
            self.assertIn(f"| {cat} |", md)

    def test_a_production_named_by_no_case_is_listed_as_a_gap(self):
        keep = [c for c in OWN if "187" not in c.productions]
        md = coverage.render(keep)
        self.assertNotIn("| 187 |", md)
        self.assertIn("187", md.split("## Cases per covered production")[0])

    def test_error_paths_are_counted_per_category(self):
        n = coverage.error_categories(OWN)
        for cat in coverage.CATEGORIES:
            self.assertGreater(n[cat], 0, cat)

    def test_ranges(self):
        self.assertEqual(coverage._ranges([1, 2, 3, 7, 9, 10]), "1-3, 7, 9-10")
        self.assertEqual(coverage._ranges([]), "")


if __name__ == "__main__":
    unittest.main()
