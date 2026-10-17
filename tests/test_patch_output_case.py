"""The large-append case: its recipe-built result, its bound and the way altering the recipe is caught."""

import copy
import json
import unittest

import support
from oneyaml_kit import cases as C
from oneyaml_kit.runner import execute

ID = "own:safety/patch-many-large-appends-into-a-created-flow-sequence"
SIBLING = "own:safety/patch-many-appends-into-a-created-flow-sequence"
CASES = {c.id: c for c in C.load_own(include_slow=True)}
X = "x" * 640
OPS = 100000


def reference() -> bytes:
    return ("a: 1\nv: " + X + "\nf: [" + ", ".join([X] * OPS) + "]\n").encode()


class LargeAppends(unittest.TestCase):
    def test_the_result_is_the_one_the_conventions_give(self):
        c = CASES[ID]
        self.assertEqual(c.expect["accept"]["bytes"], reference())
        self.assertEqual(c.input, ("a: 1\nv: " + X + "\n").encode())

    def test_the_patch_is_one_add_and_a_hundred_thousand_copies_of_one_value(self):
        ops = CASES[ID].args["patch"]
        self.assertEqual(ops[0], {"op": "add", "path": "/f", "value": []})
        self.assertEqual(len(ops), OPS + 1)
        self.assertTrue(all(o == {"op": "copy", "from": "/v", "path": "/f/-"} for o in ops[1:]))

    def test_it_is_the_larger_shape_of_the_sibling_and_a_slow_case(self):
        c, sib = CASES[ID], CASES[SIBLING]
        # Same shape: one add of an empty sequence, then copies of /v onto the end of it.
        self.assertEqual(c.args["patch"][0], sib.args["patch"][0])
        self.assertEqual(set(map(json.dumps, c.args["patch"][1:])), set(map(json.dumps, sib.args["patch"][1:])))
        self.assertIn("slow", c.tags)
        self.assertNotIn("slow", sib.tags)
        self.assertGreater(len(c.expect["accept"]["bytes"]), 1_000_000)

    def test_the_bound_is_a_hang_guard_and_the_result_stays_inside_one_recipe(self):
        # The time limit is a hang guard of 80 seconds. What gives it force is the size of the work a
        # reader that passes over the collection for every operation does (see
        # test_super_linear_cases), not a rate here. The result is built from one recipe, so it
        # cannot grow past the recipe limit.
        c = CASES[ID]
        self.assertEqual(c.expect["time_seconds"], 80)
        self.assertLessEqual(len(c.expect["accept"]["bytes"]), 64 * 1024 * 1024)
        self.assertEqual(c.expect["category"], ["limit"])

    def test_the_bytes_and_the_recipe_that_builds_them_are_checked(self):
        # An oracle that answers with the case's own bytes passes; the same case with an altered
        # recipe expects other bytes, and the answer from the original fails it.
        c = CASES[ID]
        raw = json.loads((C.OWN_DIR / "safety" / c.id.split("/")[1] / "case.json").read_bytes())
        recipe = raw["expect"]["accept"]["recipe"]
        self.assertEqual(sum(p.get("repeat", 1) for p in recipe if p["text"] == X + ", "), OPS - 1)
        altered = copy.copy(c)
        altered.expect = copy.deepcopy(c.expect)
        # Drop one repeat of the recipe: the built bytes lose one item.
        recipe2 = copy.deepcopy(recipe)
        recipe2[1]["repeat"] = OPS - 2
        self.assertNotEqual(C.generate_input(recipe2, "t", "recipe"), c.expect["accept"]["bytes"])
        # The run below changes the first part instead: the comparison then stops at the first byte
        # instead of walking a result of tens of megabytes before it finds the end.
        recipe3 = copy.deepcopy(recipe)
        recipe3[0]["text"] = recipe3[0]["text"].replace("a: 1", "a: 2", 1)
        altered.expect["accept"]["bytes"] = C.generate_input(recipe3, "t", "recipe")
        self.assertNotEqual(altered.expect["accept"]["bytes"], c.expect["accept"]["bytes"])
        oracle = support.OracleAdapter([c])
        good = execute(oracle, set(support.ALL_CAPS), [c])[0]
        self.assertEqual((good["loose"], good["strict"]), ("pass", "pass"))
        bad = execute(support.OracleAdapter([c]), set(support.ALL_CAPS), [altered])[0]
        self.assertEqual((bad["loose"], bad["strict"]), ("fail", "fail"))


if __name__ == "__main__":
    unittest.main()
