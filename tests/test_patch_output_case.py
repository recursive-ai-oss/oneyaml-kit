"""The large-append case: its recipe-built result, its bound and the way altering the recipe is caught."""

import copy
import json
import unittest

import support
from oneyaml_kit import cases as C
from oneyaml_kit.runner import execute

ID = "own:safety/patch-many-large-appends-into-a-created-flow-sequence"
SIBLING = "own:safety/patch-many-appends-into-a-created-flow-sequence"
BIG = "own:safety/one-line-flow-mapping-ten-megabytes"
CASES = {c.id: c for c in C.load_own(include_slow=True)}
X = "x" * 1024


def reference() -> bytes:
    return ("a: 1\nv: " + X + "\nf: [" + ", ".join([X] * 4000) + "]\n").encode()


class LargeAppends(unittest.TestCase):
    def test_the_result_is_the_one_the_conventions_give(self):
        c = CASES[ID]
        self.assertEqual(c.expect["accept"]["bytes"], reference())
        self.assertEqual(c.input, ("a: 1\nv: " + X + "\n").encode())

    def test_the_patch_is_one_add_and_four_thousand_copies_of_one_value(self):
        ops = CASES[ID].args["patch"]
        self.assertEqual(ops[0], {"op": "add", "path": "/f", "value": []})
        self.assertEqual(len(ops), 4001)
        self.assertTrue(all(o == {"op": "copy", "from": "/v", "path": "/f/-"} for o in ops[1:]))

    def test_it_is_the_larger_shape_of_the_sibling_and_a_slow_case(self):
        c, sib = CASES[ID], CASES[SIBLING]
        self.assertEqual(c.args["patch"][1:], sib.args["patch"][1:])
        self.assertIn("slow", c.tags)
        self.assertNotIn("slow", sib.tags)
        self.assertGreater(len(c.expect["accept"]["bytes"]), 1_000_000)

    def test_the_bound_is_the_siblings_rate_with_a_factor_of_two(self):
        # The ten-megabyte sibling allows 100 s for 18.8 MB of events, but its flow collection is about
        # 10 MB: about 10 s per megabyte. The result here is 4.1 MB, so 41 s, and the bound is twice that.
        c, big = CASES[ID], CASES[BIG]
        rate = big.expect["time_seconds"] / 10.0
        megabytes = len(c.expect["accept"]["bytes"]) / 1e6
        self.assertAlmostEqual(rate, 10.0)
        self.assertEqual(c.expect["time_seconds"], 80)
        self.assertGreaterEqual(c.expect["time_seconds"], 2 * rate * megabytes - 5)
        self.assertLessEqual(c.expect["time_seconds"], 2 * rate * megabytes + 5)
        self.assertEqual(c.expect["category"], ["limit"])

    def test_the_bytes_and_the_recipe_that_builds_them_are_checked(self):
        # An oracle that answers with the case's own bytes passes; the same case with an altered
        # recipe expects other bytes, and the answer from the original fails it.
        c = CASES[ID]
        raw = json.loads((C.OWN_DIR / "safety" / c.id.split("/")[1] / "case.json").read_bytes())
        recipe = raw["expect"]["accept"]["recipe"]
        self.assertEqual(sum(p.get("repeat", 1) for p in recipe if p["text"] == X + ", "), 3999)
        altered = copy.copy(c)
        altered.expect = copy.deepcopy(c.expect)
        # Drop one repeat of the recipe: the built bytes lose one item.
        recipe2 = copy.deepcopy(recipe)
        recipe2[1]["repeat"] = 3998
        altered.expect["accept"]["bytes"] = C.generate_input(recipe2, "t", "recipe")
        self.assertNotEqual(altered.expect["accept"]["bytes"], c.expect["accept"]["bytes"])
        oracle = support.OracleAdapter([c])
        good = execute(oracle, set(support.ALL_CAPS), [c])[0]
        self.assertEqual((good["loose"], good["strict"]), ("pass", "pass"))
        bad = execute(support.OracleAdapter([c]), set(support.ALL_CAPS), [altered])[0]
        self.assertEqual((bad["loose"], bad["strict"]), ("fail", "fail"))


if __name__ == "__main__":
    unittest.main()
