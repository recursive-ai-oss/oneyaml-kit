import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import support
from oneyaml_kit import cases as C


def tmpdir():
    return Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))


class OwnCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {c.id: c for c in C.load_own()}

    def test_corpus_covers_the_required_categories(self):
        cats = {c.category for c in self.cases.values()}
        for need in ("parse", "schema", "edit", "diagnostics", "safety"):
            self.assertIn(need, cats)

    def test_reference_case_a_is_exact(self):
        c = self.cases["own:edit/block-literal-keeps-style-with-newline"]
        self.assertEqual(c.input, b"#hello\na: 10\n\n#abc 123\nb: |\n  this is a test\nc: 13\n")
        self.assertEqual(c.args["patch"], [{"op": "replace", "path": "/b", "value": "this is a test2\n"}])
        self.assertEqual(c.expect["bytes"], b"#hello\na: 10\n\n#abc 123\nb: |\n  this is a test2\nc: 13\n")

    def test_reference_case_b_is_exact(self):
        c = self.cases["own:edit/block-literal-strip-when-no-trailing-newline"]
        self.assertEqual(c.args["patch"], [{"op": "replace", "path": "/b", "value": "this is a test2"}])
        self.assertEqual(c.expect["bytes"], b"#hello\na: 10\n\n#abc 123\nb: |-\n  this is a test2\nc: 13\n")

    def test_bytes_survive_exactly(self):
        bom = self.cases["own:parse/bom-and-crlf"].input
        self.assertTrue(bom.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", bom)
        u16 = self.cases["own:parse/utf-16le-with-bom"].input
        self.assertEqual(u16[:2], b"\xff\xfe")
        self.assertEqual(u16[2:].decode("utf-16-le"), "a: b\n")
        self.assertEqual(self.cases["own:safety/invalid-utf-8-byte"].input, b"a: \xff\n")
        crlf = self.cases["own:edit/bom-and-crlf-kept"]
        self.assertEqual(crlf.input, b"\xef\xbb\xbfa: 1\r\nb: 2\r\n")
        self.assertEqual(crlf.expect["bytes"], b"\xef\xbb\xbfa: 3\r\nb: 2\r\n")
        for c in self.cases.values():
            d = C.OWN_DIR / c.id[len("own:"):]
            self.assertEqual(c.input, (d / "input.yaml").read_bytes(), c.id)

    def test_spec_cases_cite_something(self):
        for c in self.cases.values():
            if c.origin == "spec":
                self.assertTrue(c.refs, c.id)

    def test_every_patch_case_targets_the_patch_operation(self):
        for c in self.cases.values():
            if c.category == "edit":
                self.assertEqual(c.op, "patch", c.id)


class VendoredSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = C.load_yts()

    def test_counts_match_the_release(self):
        ev = [c for c in self.cases if c.op == "events"]
        self.assertEqual(len(ev), 402)
        self.assertEqual(sum(1 for c in ev if c.expect["kind"] == "error"), 94)
        self.assertEqual(sum(1 for c in ev if c.expect["kind"] == "events"), 308)

    def test_value_cases_exist_only_for_valid_inputs(self):
        for c in self.cases:
            if c.op == "value":
                self.assertFalse((C.YTS_DIR / c.id[4:-6] / "error").exists(), c.id)

    def test_source_record(self):
        s = (C.SUITES / "yaml-test-suite" / "SOURCE.md").read_text()
        self.assertIn("data-2022-01-17", s)
        revision = "6e6c296ae9c9d2d5c4134b4b64d01b29ac19ff6f"
        self.assertIn(revision, s)
        self.assertIn("MIT", s)
        self.assertTrue((C.SUITES / "yaml-test-suite" / "LICENSE").is_file())

    def test_corrections_differ_from_upstream_and_carry_reasons(self):
        by_id = {c.id: c for c in self.cases}
        corr = C.load_corrections()
        self.assertEqual(len(corr), 12)
        for cid, e in corr.items():
            c = by_id[cid]
            self.assertIsNotNone(c.correction, cid)
            key = "events" if "events" in e else "value"
            self.assertNotEqual(json.dumps(c.correction["expect"][key]), json.dumps(c.expect[key]), cid)
            self.assertTrue(e["reason"] and e["citation"], cid)
            self.assertIn("YAML 1.2.2", e["citation"])

    def test_a_correction_for_an_unknown_case_is_rejected(self):
        with self.assertRaises(C.CaseError):
            C.load_yts(corrections={"yts:ZZZZ": {"case": "yts:ZZZZ", "events": [], "reason": "r", "citation": "c"}})

    def test_a_correction_of_a_valid_case_is_rejected(self):
        with self.assertRaises(C.CaseError):
            C.load_yts(corrections={"yts:229Q": {"case": "yts:229Q", "events": [], "reason": "r", "citation": "c"}})

    def test_exact_typing_is_the_default_and_only_one_value_is_corrected(self):
        values = [c for c in self.cases if c.op == "value"]
        self.assertEqual(len(values), 279)
        self.assertTrue(all(c.expect["tags"] == "ignore" and "numbers" not in c.expect for c in values))
        self.assertEqual([c.id for c in values if c.correction], ["yts:UGM3@value"])

    def test_the_revision_is_read_from_the_source_record(self):
        self.assertEqual(C.yts_revision(), "6e6c296ae9c9d2d5c4134b4b64d01b29ac19ff6f")
        self.assertEqual(self.cases[0].provenance["revision"], C.yts_revision())

    def test_dollar_keys_in_upstream_json_are_rewritten_to_the_map_form(self):
        self.assertEqual(C._to_kit_value({"$float": 1, "a": [{"$map": 2}]}),
                         {"$map": [["$float", 1], ["a", [{"$map": [["$map", 2]]}]]]})

    def test_in_json_streams_decode(self):
        self.assertEqual(C._decode_json_stream(b'{"a": 1}\n[2]\n', "t"), [{"a": 1}, [2]])
        self.assertEqual(C._decode_json_stream(b"", "t"), [])


class Pins(unittest.TestCase):
    def test_pins_match_the_tree(self):
        pinned = json.loads((support.ROOT / "tests" / "pins.json").read_bytes())
        cur = C.current_pins()
        self.assertEqual(pinned["yaml-test-suite"], cur["yaml-test-suite"],
                         "vendored data or corrections changed; run `bin/kit pins --write` and check the diff")
        self.assertEqual(pinned["own"]["cases"], cur["own"]["cases"],
                         "a kit case was added, removed or changed; run `bin/kit pins --write` and check the diff")

    def test_removing_a_case_changes_the_digests(self):
        t = tmpdir()
        try:
            shutil.copytree(C.OWN_DIR, t / "cases")
            before = C.own_case_digests(t / "cases")
            self.assertEqual(before, json.loads((support.ROOT / "tests" / "pins.json").read_bytes())["own"]["cases"])
            shutil.rmtree(t / "cases" / "edit" / "remove-key")
            self.assertNotEqual(before, C.own_case_digests(t / "cases"))
        finally:
            shutil.rmtree(t)

    def test_altering_an_expected_file_changes_only_its_digest(self):
        t = tmpdir()
        try:
            shutil.copytree(C.OWN_DIR, t / "cases")
            before = C.own_case_digests(t / "cases")
            f = t / "cases" / "edit" / "remove-key" / "expected.yaml"
            f.write_bytes(f.read_bytes() + b" ")
            after = C.own_case_digests(t / "cases")
            self.assertEqual([k for k in before if before[k] != after[k]], ["edit/remove-key"])
        finally:
            shutil.rmtree(t)

    def test_altering_vendored_data_changes_the_tree_digest(self):
        t = tmpdir()
        try:
            shutil.copytree(C.YTS_DIR / "229Q", t / "229Q")
            before = C.tree_digest(t)
            p = t / "229Q" / "test.event"
            p.write_bytes(p.read_bytes() + b"x")
            self.assertNotEqual(before, C.tree_digest(t))
        finally:
            shutil.rmtree(t)


class Validation(unittest.TestCase):
    """The loader refuses malformed cases instead of quietly weakening them."""

    GOOD = {
        "format": "oneyaml-kit-case/2", "id": "parse/x", "title": "t", "category": "parse",
        "origin": "bug", "operation": {"type": "events"},
        "expect": {"kind": "error", "category": "syntax"},
        "provenance": {"reference": "written for the tests"},
    }

    def load(self, d, rel="parse/x"):
        t = tmpdir()
        try:
            cd = t / "cases" / rel
            cd.mkdir(parents=True)
            (cd / "case.json").write_text(json.dumps(d))
            (cd / "input.yaml").write_bytes(b"a\n")
            return C.load_own_case(cd, t / "cases")
        finally:
            shutil.rmtree(t)

    def bad(self, **change):
        d = copy.deepcopy(self.GOOD)
        for k, v in change.items():
            if v is None:
                d.pop(k, None)
            else:
                d[k] = v
        with self.assertRaises(C.CaseError, msg=str(change)):
            self.load(d)

    def test_good_case_loads(self):
        self.assertEqual(self.load(self.GOOD).id, "own:parse/x")

    def test_rejections(self):
        self.bad(extra_key=1)
        self.bad(format="other")
        self.bad(id="parse/y")
        self.bad(category="nope")
        self.bad(origin="nope")
        self.bad(title=None)
        self.bad(operation={"type": "nope"})
        self.bad(operation={"type": "events", "stray": 1})
        self.bad(expect={"kind": "error", "category": "nope"})
        self.bad(expect={"kind": "error", "column": 3})
        self.bad(expect={"kind": "error", "line": 0})
        self.bad(expect={"kind": "error", "stray": 1})
        self.bad(expect={"kind": "bytes", "file": "missing.yaml"})
        self.bad(expect={"kind": "bytes", "file": "../input.yaml"})
        self.bad(expect={"kind": "error", "unchanged": True})
        self.bad(expect={"kind": "terminates"})
        self.bad(expect={"kind": "bytes", "file": "input.yaml"})
        self.bad(origin="spec")  # a spec case must cite its source

    def test_value_escapes_are_reserved(self):
        base = {**self.GOOD, "operation": {"type": "value"}}

        def val(v):
            d = copy.deepcopy(base)
            d["expect"] = {"kind": "value", "value": v}
            return d

        self.load(val([{"$float": "inf"}, {"$map": [[1, 2]]}, {"$tagged": {"tag": "!t", "value": 1}}]))
        for bad in ([{"$float": "inf", "x": 1}], [{"$float": "big"}], [{"$other": 1}], [{"$map": [[1]]}],
                    [{"$tagged": {"tag": "!t"}}], [{"$map": 1}], [float("inf")]):
            with self.assertRaises(C.CaseError, msg=str(bad)):
                self.load(val(bad))

    def test_numbers_json_mode_is_gone(self):
        d = copy.deepcopy(self.GOOD)
        d["operation"] = {"type": "value"}
        d["expect"] = {"kind": "value", "value": [1], "numbers": "json"}
        with self.assertRaises(C.CaseError):
            self.load(d)
        d["expect"] = {"kind": "value", "value": [1], "tags": "ignore"}
        with self.assertRaises(C.CaseError):
            self.load(d)

    def test_bounded_expectations(self):
        d = copy.deepcopy(self.GOOD)
        d.update(origin="spec", refs=["x"])
        good = {"kind": "bounded", "time_seconds": 5, "memory_mb": 100, "error": "limit"}
        d["expect"] = good
        self.assertEqual(self.load(d).expect["category"], ["limit"])
        for change in ({"time_seconds": 0}, {"memory_mb": -1}, {"error": "nope"}, {"error": "any"},
                       {"error": ["any"]}, {"error": []}, {"error": ["limit", "limit"]},
                       {"error": None, "accept": None},
                       {"stray": 1}, {"accept": {"kind": "bytes", "file": "input.yaml"}},
                       {"accept": {"kind": "error"}}):
            e = {**good, **change}
            e = {k: v for k, v in e.items() if v is not None}
            d["expect"] = e
            with self.assertRaises(C.CaseError, msg=str(change)):
                self.load(d)
        d["expect"] = {k: v for k, v in good.items() if k != "time_seconds"}
        with self.assertRaises(C.CaseError):
            self.load(d)

    def test_bounded_may_allow_a_list_of_error_categories(self):
        d = copy.deepcopy(self.GOOD)
        d.update(origin="spec", refs=["x"])
        d["expect"] = {"kind": "bounded", "time_seconds": 5, "memory_mb": 100, "error": ["syntax", "limit"]}
        self.assertEqual(self.load(d).expect["category"], ["syntax", "limit"])
        d["expect"]["error"] = "limit"
        self.assertEqual(self.load(d).expect["category"], ["limit"])

    def test_provenance(self):
        d = copy.deepcopy(self.GOOD)
        d.pop("provenance")
        with self.assertRaises(C.CaseError):
            self.load(d)  # a bug case says where the bug is from
        d["provenance"] = {"reference": "issue 12 of some project"}
        self.assertEqual(self.load(d).provenance, {"reference": "issue 12 of some project"})
        d["provenance"] = {"reference": "x", "stray": "y"}
        with self.assertRaises(C.CaseError):
            self.load(d)
        d["origin"] = "real-world"
        d["provenance"] = {"source": "https://example.org/a.yaml"}
        with self.assertRaises(C.CaseError):
            self.load(d)  # real-world data records its licence too
        d["provenance"] = {"source": "https://example.org/a.yaml", "licence": "MIT"}
        self.load(d)

    def test_unknown_patch_operation_names_are_allowed(self):
        d = copy.deepcopy(self.GOOD)
        patch = [{"op": "frobnicate", "path": "/a"}]
        d.update(category="edit", id="edit/x", operation={"type": "patch", "patch": patch},
                 expect={"kind": "error", "category": "patch-invalid", "unchanged": True})
        self.assertEqual(self.load(d, rel="edit/x").op, "patch")

    def test_category_must_match_directory(self):
        d = copy.deepcopy(self.GOOD)
        d["id"] = "edit/x"
        with self.assertRaises(C.CaseError):
            self.load(d, rel="edit/x")

    def test_patch_needs_a_patch(self):
        d = copy.deepcopy(self.GOOD)
        d.update(category="edit", id="edit/x", operation={"type": "patch"})
        with self.assertRaises(C.CaseError):
            self.load(d, rel="edit/x")

    def test_split_lines_only_splits_on_newline(self):
        self.assertEqual(C.split_lines("a\u2028b\nc\n"), ["a\u2028b", "c"])
        self.assertEqual(C.split_lines(""), [])


if __name__ == "__main__":
    unittest.main()
