"""Edit cases whose layout the specification leaves open, and the tag that says so."""

import json
import unittest

import support
from oneyaml_kit import cases as C
from oneyaml_kit import compare as K

CASES = {c.id: c for c in C.load_own()}
FLAT = "own:edit/add-sequence-value-defaults-to-two-spaces"
ENCLOSING = "own:edit/add-sequence-value-uses-the-enclosing-mappings-step"
SCALARS = ("own:edit/set-collection-to-scalar", "own:edit/set-sequence-to-scalar")
CAPS = set(support.ALL_CAPS)


def notes(c):
    where = support.ROOT / "suites" / "own" / "cases" / c.id.split(":", 1)[1]
    return json.loads((where / "case.json").read_bytes())["notes"]


def verdict(c, out: bytes):
    r = K.evaluate(c, c.expect, {"status": "ok", "output": C.b64(out)}, CAPS)
    return r["loose"], r["strict"]


class SequenceIndentation(unittest.TestCase):
    def test_a_flat_document_gets_two_spaces_and_no_nested_mapping(self):
        c = CASES[FLAT]
        self.assertEqual(c.input, b"a: 1\n")
        self.assertEqual(c.args["patch"], [{"op": "add", "path": "/n", "value": [1]}])
        self.assertEqual(c.expect["bytes"], b"a: 1\nn:\n  - 1\n")
        self.assertIn("convention", c.tags)

    def test_the_enclosing_mappings_step_wins_over_an_earlier_mapping(self):
        c = CASES[ENCLOSING]
        self.assertEqual(c.input, b"x:\n    y: 1\nz:\n  w: 1\n")
        self.assertEqual(c.args["patch"], [{"op": "add", "path": "/z/n", "value": [1]}])
        self.assertEqual(c.expect["bytes"], b"x:\n    y: 1\nz:\n  w: 1\n  n:\n    - 1\n")
        self.assertIn("convention", c.tags)

    def test_the_cases_separate_the_readings(self):
        # Four spaces from the earlier mapping, or the dash flush with the key, must fail.
        c = CASES[ENCLOSING]
        for wrong in (b"x:\n    y: 1\nz:\n  w: 1\n  n:\n      - 1\n",
                      b"x:\n    y: 1\nz:\n  w: 1\n  n:\n  - 1\n"):
            self.assertNotEqual(verdict(c, wrong), verdict(c, c.expect["bytes"]))
        f = CASES[FLAT]
        for wrong in (b"a: 1\nn:\n- 1\n", b"a: 1\nn:\n    - 1\n"):
            self.assertNotEqual(verdict(f, wrong), verdict(f, f.expect["bytes"]))

    def test_the_docs_state_the_reading_the_cases_pin(self):
        doc = " ".join((support.ROOT / "docs" / "edit-semantics.md").read_text(encoding="utf-8").split())
        self.assertIn("the enclosing mapping's own step wins over the step used elsewhere in the document", doc)
        self.assertIn("The step is two spaces when the document has no nested block mapping", doc)
        for cid in (FLAT, ENCLOSING):
            self.assertIn("docs/edit-semantics.md", notes(CASES[cid]))


class CollectionToScalar(unittest.TestCase):
    def test_both_cases_carry_the_tag_and_name_the_other_layout(self):
        for cid in SCALARS:
            c = CASES[cid]
            self.assertIn("convention", c.tags, cid)
            self.assertIn("194", notes(c), cid)
            self.assertIn("convention", notes(c).lower(), cid)
        self.assertEqual(CASES[SCALARS[0]].expect["bytes"], b"a: 5\nb: 3\n")
        self.assertEqual(CASES[SCALARS[1]].expect["bytes"], b"a: x\nb: 3\n")

    def test_the_docs_list_the_rule(self):
        doc = (support.ROOT / "docs" / "edit-semantics.md").read_text(encoding="utf-8")
        self.assertIn("A collection replaced by a scalar stays on the key's line", doc)


if __name__ == "__main__":
    unittest.main()
