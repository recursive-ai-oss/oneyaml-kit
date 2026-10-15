"""Malformed UTF-8 after a character that quoted scalars allow but that is not printable, and the
comments around an entry that is removed after a block scalar."""

import unittest

import support
from oneyaml_kit import cases as C
from oneyaml_kit import compare as K

CASES = {c.id: c for c in C.load_own()}
CAPS = set(support.ALL_CAPS)
UTF8 = ("own:parse/invalid-utf8-after-del-in-quoted-scalar",
        "own:parse/invalid-utf8-after-c1-in-quoted-scalar",
        "own:parse/invalid-utf8-after-noncharacter-in-double-quoted-scalar")
EDIT = "own:edit/remove-entry-after-block-scalar-with-hash-lines"


def verdict(c, resp):
    r = K.evaluate(c, c.expect, resp, CAPS)
    return r["loose"], r["strict"]


class InvalidUtf8AfterUnprintable(unittest.TestCase):
    def test_the_bytes_are_not_utf8_and_the_column_is_the_first_bad_byte(self):
        for cid in UTF8:
            c = CASES[cid]
            with self.assertRaises(UnicodeDecodeError, msg=cid) as ctx:
                c.input.decode("utf-8")
            before = c.input[:ctx.exception.start].decode("utf-8")
            # Everything before the fault is a valid prefix: nb-json characters (tab, U+0020 and up),
            # one of which is not printable, and no line break.
            self.assertTrue(all(ch == "\t" or ord(ch) >= 0x20 for ch in before), cid)
            self.assertTrue(any(ch in "\x7f\u0080\ufffe" for ch in before), cid)
            self.assertNotIn("\n", before)
            self.assertEqual((c.expect["category"], c.expect["line"], c.expect["column"]),
                             ("encoding", 1, len(before) + 1), cid)
            self.assertEqual(c.origin, "bug")

    def test_the_inputs_are_the_ones_that_were_reported(self):
        self.assertEqual(CASES[UTF8[0]].input, b"a: '\x7f\x8e'\n")
        self.assertEqual(CASES[UTF8[1]].input, b"a: '\xc2\x80\xef'\n")
        self.assertEqual(CASES[UTF8[2]].input, b"- \"\xef\xbf\xbe\xff\"\n")
        self.assertEqual([CASES[i].expect["column"] for i in UTF8], [6, 6, 5])

    def test_only_an_encoding_error_at_that_column_passes_strictly(self):
        for cid in UTF8:
            c = CASES[cid]
            col = c.expect["column"]
            ok = {"status": "error", "error": {"category": "encoding", "line": 1, "column": col}}
            self.assertEqual(verdict(c, ok), ("pass", "pass"), cid)
            # An earlier or later column, or another category, is an error but not this one.
            for err in ({"category": "encoding", "line": 1, "column": col - 1},
                        {"category": "encoding", "line": 1, "column": col + 1},
                        {"category": "syntax", "line": 1, "column": col}):
                self.assertEqual(verdict(c, {"status": "error", "error": err}), ("pass", "fail"), (cid, err))
            # Taking the malformed text as valid is the failure the cases exist for.
            self.assertEqual(verdict(c, {"status": "ok", "events": ["+STR", "-STR"]})[0], "fail", cid)


class RemoveEntryAfterBlockScalar(unittest.TestCase):
    def test_the_hash_line_inside_the_scalar_stays_and_the_comment_above_the_entry_goes(self):
        c = CASES[EDIT]
        self.assertEqual(c.input, b"a: |\n  x\n  # in\n# h\nb: 1\nc: 2\n")
        self.assertEqual(c.expect["bytes"], b"a: |\n  x\n  # in\nc: 2\n")
        self.assertEqual([o["op"] for o in c.args["patch"]], ["remove"])
        self.assertEqual(c.args["patch"][0]["path"], "/b")
        self.assertIn("convention", c.tags)
        self.assertIn("comment-ownership", c.tags)
        self.assertEqual(c.origin, "bug")
        self.assertIn("170", c.productions)

    def test_the_comment_above_goes_with_the_entry_only_by_the_convention_in_the_docs(self):
        doc = (support.ROOT / "docs" / "edit-semantics.md").read_text(encoding="utf-8")
        self.assertIn("a comment on the line directly\nabove it with no blank line between", doc)

    def test_losing_the_scalar_line_or_keeping_the_comment_fails(self):
        c = CASES[EDIT]
        ok = {"status": "ok", "output": C.b64(c.expect["bytes"])}
        self.assertEqual(verdict(c, ok), ("pass", "pass"))
        for bad in (b"a: |\n  x\nc: 2\n", b"a: |\n  x\n  # in\n# h\nc: 2\n", b"a: |\n  x\n  # in\n\nc: 2\n",
                    b"a: |\n  x\n# h\nc: 2\n"):
            self.assertEqual(verdict(c, {"status": "ok", "output": C.b64(bad)}), ("fail", "fail"), bad)


if __name__ == "__main__":
    unittest.main()
