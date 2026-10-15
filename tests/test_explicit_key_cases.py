"""Two edit cases about an explicit key whose content starts on the line after the `?`."""

import unittest

import support
from oneyaml_kit import cases as C
from oneyaml_kit import compare as K
from oneyaml_kit import readback as R

CASES = {c.id: c for c in C.load_own()}
PATCH = "own:edit/patch-after-explicit-key-with-key-on-next-line-and-empty-value"
FILL = "own:edit/set-empty-value-of-explicit-key-on-next-line"
CAPS = set(support.ALL_CAPS)


def read(data: bytes):
    return R.to_value(R.parse(data.decode()), R.SCHEMAS["core"])


def verdict(c, out: bytes):
    r = K.evaluate(c, c.expect, {"status": "ok", "output": C.b64(out)}, CAPS)
    return r["loose"], r["strict"]


class ExplicitKeyOnNextLine(unittest.TestCase):
    def test_both_are_bug_cases_with_the_explicit_entry_productions(self):
        for cid in (PATCH, FILL):
            c = CASES[cid]
            self.assertEqual(c.origin, "bug", cid)
            for p in ("189", "190", "191"):
                self.assertIn(p, c.productions, cid)
        self.assertIn("convention", CASES[PATCH].tags)
        self.assertNotIn("convention", CASES[FILL].tags)

    def test_the_patch_case_is_past_the_region_threshold_and_the_entry_has_its_key_on_the_next_line(self):
        c = CASES[PATCH]
        text = c.input.decode()
        self.assertGreater(len(text), 1024)
        self.assertEqual(text.count(": 0\n"), 200)
        self.assertTrue(text.endswith("x:\n  ?\n    a\n  :\n  b: 1\n"))
        self.assertEqual([o["op"] for o in c.args["patch"]], ["add", "remove", "add"])

    def test_the_expected_bytes_read_back_as_the_patch_says(self):
        c = CASES[PATCH]
        before, after = read(c.input), read(c.expect["bytes"])
        self.assertEqual(before["x"], {"a": None, "b": 1})
        self.assertEqual(after["x"], {"a": None})
        self.assertEqual(after["nk1"], [1])
        self.assertEqual({k: v for k, v in after.items() if k not in ("x", "nk1")},
                         {k: v for k, v in before.items() if k != "x"})
        self.assertEqual(list(after)[-2:], ["x", "nk1"])  # the new member is the last
        # Only `  b: 1` is gone and the new member is added: every other line is the same.
        self.assertEqual(c.expect["bytes"].decode(), c.input.decode().replace("  b: 1\n", "") + "nk1:\n  - 1\n")

    def test_the_fill_case_puts_the_value_after_the_colon_that_is_there(self):
        c = CASES[FILL]
        self.assertEqual(c.input, b"?\n  a\n:\nb: 1\n")
        self.assertEqual(c.expect["bytes"], b"?\n  a\n: 1\nb: 1\n")
        self.assertEqual(read(c.input), {"a": None, "b": 1})
        self.assertEqual(read(c.expect["bytes"]), {"a": 1, "b": 1})

    def test_the_regressions_they_guard_are_failures(self):
        patch, fill = CASES[PATCH], CASES[FILL]
        good = verdict(patch, patch.expect["bytes"])
        self.assertEqual(good, ("pass", "pass"))
        text = patch.expect["bytes"].decode()
        # The colon of the explicit entry moved behind the new member.
        moved = text.replace("  :\nnk1:\n  - 1\n", "nk1:\n  - 1\n  :\n").encode()
        self.assertEqual(verdict(patch, moved), ("fail", "fail"))
        # The explicit entry lost its colon, or became an implicit one.
        for bad in (text.replace("  :\nnk1", "nk1"), text.replace("?\n    a\n  :\n", "a:\n")):
            self.assertEqual(verdict(patch, bad.encode()), ("fail", "fail"))
        # The sequence flush with its key is another layout than the case states.
        self.assertEqual(verdict(patch, text.replace("  - 1", "- 1").encode()), ("fail", "fail"))
        self.assertEqual(verdict(fill, fill.expect["bytes"]), ("pass", "pass"))
        for bad in (b"?\n  a\n:\n  1\nb: 1\n", b"?\n  a\n:\n1\nb: 1\n", b"a: 1\nb: 1\n", b"?\n  a\n: 1\n: 1\nb: 1\n"):
            self.assertEqual(verdict(fill, bad), ("fail", "fail"), bad)

    def test_the_documentation_states_both_rules(self):
        doc = (support.ROOT / "docs" / "edit-semantics.md").read_text(encoding="utf-8")
        self.assertIn("When the entry already has its `:` line with nothing after it", doc)
        self.assertIn("When the document has none", doc)


if __name__ == "__main__":
    unittest.main()
