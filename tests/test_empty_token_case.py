"""The case where two keys have the empty text and the pointer `/` therefore matches both."""

import unittest

import support
from oneyaml_kit import cases as C
from oneyaml_kit import compare as K
from oneyaml_kit import readback as R

CAPS = set(support.ALL_CAPS)
ID = "own:edit/refuse-ambiguous-empty-token"


def case():
    return {c.id: c for c in C.load_own()}[ID]


def verdict(c, resp):
    r = K.evaluate(c, c.expect, dict(resp), CAPS)
    return r["loose"], r["strict"]


class AmbiguousEmptyToken(unittest.TestCase):
    def test_the_input_holds_a_quoted_empty_key_and_a_null_key(self):
        root = R.parse(case().input.decode())
        keys = [(k.text, k.style, k.implicit_empty) for k, _ in root.pairs]
        self.assertEqual([k[0] for k in keys].count(""), 2)
        self.assertIn(("", "double", False), keys)
        self.assertTrue(any(k[0] == "" and k[2] for k in keys), keys)

    def test_the_pointer_is_the_empty_token_and_the_document_stays_as_it_was(self):
        c = case()
        self.assertEqual(c.args["patch"][0]["path"], "/")
        self.assertEqual(c.expect["kind"], "error")
        self.assertEqual(c.expect["category"], "refused-ambiguous")
        self.assertTrue(c.expect["unchanged"])

    def test_only_a_refusal_of_that_kind_that_leaves_the_document_passes(self):
        c = case()
        same = C.b64(c.input)
        refused = {"status": "error", "error": {"category": "refused-ambiguous"}, "output": same}
        self.assertEqual(verdict(c, refused), ("pass", "pass"))
        # A refusal of another kind is an error but not the one the case names.
        other = {"status": "error", "error": {"category": "patch-path"}, "output": same}
        self.assertEqual(verdict(c, other), ("pass", "fail"))
        # A refusal that reports a changed document breaks the all-or-nothing rule.
        changed = dict(refused, output=C.b64(c.input + b"x: 1\n"))
        self.assertEqual(verdict(c, changed), ("pass", "fail"))
        # Applying the patch to one of the two keys is the failure the case exists for.
        self.assertEqual(verdict(c, {"status": "ok", "output": same})[0], "fail")


if __name__ == "__main__":
    unittest.main()
