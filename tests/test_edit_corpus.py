"""The edit corpus and the specification-coverage report."""

import json
import re
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

    def test_cases_resting_on_a_convention_carry_the_tag(self):
        # A case whose notes call something a convention or leave it open is tagged, so the
        # reports count it apart. The comment tag is a second, more specific label.
        said = re.compile(r"kit convention|rests on a convention|left open|leaves [^.]{0,40}open|"
                          r"not fixed by|does not fix|gives comments no owner", re.I)
        for c in OWN:
            where = support.ROOT / "suites" / "own" / "cases" / c.id.split(":", 1)[1]
            notes = json.loads((where / "case.json").read_bytes()).get("notes", "")
            if said.search(notes):
                self.assertIn("convention", c.tags, c.id)
        tagged = [c for c in EDIT if "convention" in c.tags]
        self.assertGreaterEqual(len(tagged), 40)
        for c in EDIT:
            if "comment-ownership" in c.tags:
                self.assertIn("convention", c.tags, c.id)
            self.assertNotIn("judgement", c.tags, c.id)

    def test_convention_cases_name_their_rule_in_the_notes(self):
        for c in EDIT:
            if "convention" in c.tags:
                where = support.ROOT / "suites" / "own" / "cases" / c.id.split(":", 1)[1]
                notes = json.loads((where / "case.json").read_bytes()).get("notes", "")
                self.assertIn("convention", notes.lower(), c.id)

    def test_every_rule_of_the_kit_is_stated_in_the_docs(self):
        doc = (support.ROOT / "docs" / "edit-semantics.md").read_text(encoding="utf-8")
        self.assertIn("## Kit conventions", doc)
        for phrase in ("end of its mapping", "Strip becomes clip", "flow collection", "single-quoted"):
            self.assertIn(phrase, doc)

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


class Tags(unittest.TestCase):
    def test_tags_reach_the_result_records(self):
        from oneyaml_kit.runner import execute, validate_result
        cases = [c for c in EDIT if "convention" in c.tags][:5] + [c for c in EDIT if not c.tags][:3]
        res = execute(support.OracleAdapter(cases), set(support.ALL_CAPS), cases)
        by = {r["id"]: r for r in res}
        for c in cases:
            if c.tags:
                self.assertEqual(by[c.id]["tags"], sorted(c.tags), c.id)
            else:
                self.assertNotIn("tags", by[c.id], c.id)
        doc = {"format": "oneyaml-kit-results/2", "kit": {}, "run": {}, "adapter": {
            "name": "a", "version": "1", "language": "x", "capabilities": [], "protocol": 2},
            "cases": [dict(res[0], tags="convention")], "deviations": {}}
        self.assertTrue(any("tags must be a list" in p for p in validate_result(doc)))

    def test_yaml_test_suite_records_carry_no_tags(self):
        from oneyaml_kit.runner import execute
        yts = [c for c in ALL if c.suite == "yaml-test-suite"][:3]
        res = execute(support.OracleAdapter(yts), set(support.ALL_CAPS), yts)
        self.assertTrue(all("tags" not in r for r in res))


class Coverage(unittest.TestCase):
    def test_every_named_production_exists(self):
        prods = coverage.production_numbers(OWN)
        self.assertTrue(all(1 <= p <= coverage.TOTAL for p in prods))
        self.assertGreaterEqual(len(prods), 40)

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
        self.assertIn("Not named by any case", md)
        self.assertNotIn("With a case", md)
        self.assertIn(f"Own cases: {len(OWN)}", md)
        for cat in coverage.CATEGORIES:
            self.assertIn(f"| {cat} |", md)

    def test_the_question_mark_indicator_comes_from_the_explicit_entry_not_from_every_entry(self):
        # [188] is any block mapping entry, written with `?` or not; [189] and [190] are the explicit ones.
        self.assertNotIn("5", coverage.DERIVED.get("188", []))
        self.assertEqual(coverage.DERIVED["189"], ["5"])
        self.assertEqual(coverage.DERIVED["190"], ["5"])
        implicit = [c for c in OWN if "188" in c.productions and not {"189", "190"} & set(c.productions)]
        self.assertTrue(implicit)
        self.assertNotIn("5", coverage.derived_numbers(implicit))

    def test_a_stream_with_a_second_byte_order_mark_names_the_mark_and_the_document_prefix(self):
        # Their second document starts with its own byte order mark: [3] is the mark, [202] the prefix it belongs to.
        for c in OWN:
            if c.id.startswith("own:parse/encoding-utf-") and c.id.endswith("-without-bom"):
                self.assertTrue({"3", "202"} <= set(c.productions), c.id)

    def test_a_production_named_by_no_case_is_listed_as_a_gap(self):
        keep = [c for c in OWN if "187" not in c.productions]
        md = coverage.render(keep)
        self.assertNotIn("| 187 |", md)
        self.assertIn("187", md.split("## Cases per covered production")[0])

    def test_error_paths_are_counted_per_category(self):
        n = coverage.error_categories(OWN)
        for cat in coverage.CATEGORIES:
            self.assertGreater(n[cat], 0, cat)

    def test_derived_names_are_reported_apart_from_hand_picked_ones(self):
        # No case lists an indicator production that a construct only implies.
        for c in OWN:
            for p in c.productions:
                for implied in coverage.DERIVED.get(p, []):
                    self.assertNotIn(implied, c.productions, c.id)
        hand = set(coverage.production_numbers(OWN))
        implied = set(coverage.derived_numbers(OWN)) - hand
        self.assertTrue(implied)
        self.assertIn(f"{len(implied)} more", coverage.render(ALL))

    def test_known_label_corrections(self):
        by = {c.id: c for c in OWN}
        self.assertEqual(by["own:edit/keep-tab-after-colon"].productions, ["66"])
        self.assertIn("205", by["own:edit/keep-document-end-marker"].productions)
        self.assertNotIn("204", by["own:edit/keep-document-end-marker"].productions)
        self.assertIn("202", by["own:edit/keep-bom-replace"].productions)
        for c in OWN:
            if "3" in c.productions:
                inp = c.input
                # The mark may start the stream or a later document of it, in any of the encodings.
                self.assertTrue(any(m in inp for m in (b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")), c.id)

    def test_ranges(self):
        self.assertEqual(coverage._ranges([1, 2, 3, 7, 9, 10]), "1-3, 7, 9-10")
        self.assertEqual(coverage._ranges([]), "")


if __name__ == "__main__":
    unittest.main()
