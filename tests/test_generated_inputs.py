"""Inputs and expected results that a case describes with a recipe instead of storing."""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import support
from oneyaml_kit import cases as C


def write_case(root: Path, name: str, recipe, extra=None) -> Path:
    d = root / "safety" / name
    d.mkdir(parents=True)
    case = {
        "format": "oneyaml-kit-case/2", "id": f"safety/{name}", "title": "t", "category": "safety",
        "origin": "bug", "refs": ["x"], "provenance": {"reference": "x"},
        "operation": {"type": "events"},
        "expect": {"kind": "bounded", "time_seconds": 1, "memory_mb": 64, "error": "limit"},
    }
    if recipe is not None:
        case["input_recipe"] = recipe
    case.update(extra or {})
    (d / "case.json").write_text(json.dumps(case))
    return d


class Recipe(unittest.TestCase):
    def test_a_recipe_builds_exactly_these_bytes(self):
        out = C.generate_input([
            {"text": "{", "repeat": 3},
            {"numbers": {"count": 3, "prefix": "k", "suffix": ": v,\n", "width": 2}},
            {"numbers": {"start": 8, "count": 3, "suffix": " "}},
            {"text": "\u00e9\n"},
            {"text": "x", "repeat": 0},
        ], "t")
        self.assertEqual(out, b"{{{k00: v,\nk01: v,\nk02: v,\n8 9 10 \xc3\xa9\n")

    def test_numbers_are_all_different_and_in_order(self):
        out = C.generate_input([{"numbers": {"count": 1000, "suffix": ","}}], "t")
        parts = out.decode().split(",")[:-1]
        self.assertEqual(parts, [str(i) for i in range(1000)])

    def test_text_is_never_evaluated(self):
        text = "{__import__('os').system('x')} %s {0} \\n"
        self.assertEqual(C.generate_input([{"text": text}], "t"), text.encode())

    def test_malformed_recipes_are_refused(self):
        bad = [
            None, [], {}, "x", [1], [{}], [{"text": 5}], [{"text": "a", "repeat": -1}],
            [{"text": "a", "repeat": True}], [{"text": "a", "repeat": 1.5}],
            [{"text": "a", "also": 1}], [{"numbers": 5}], [{"numbers": {}}],
            [{"numbers": {"count": 1, "code": "x"}}], [{"numbers": {"count": -1}}],
            [{"numbers": {"count": 1, "prefix": 3}}], [{"text": "a", "numbers": {"count": 1}}],
            [{"text": "a"}] * (C.MAX_RECIPE_PARTS + 1),
        ]
        for r in bad:
            with self.assertRaises(C.CaseError, msg=repr(r)):
                C.generate_input(r, "t")

    def test_the_size_limit_is_checked_before_anything_is_built(self):
        limit = C.MAX_GENERATED_BYTES
        at = C.generate_input([{"text": "x", "repeat": limit}], "t")
        self.assertEqual(len(at), limit)
        for r in ([{"text": "x", "repeat": limit + 1}],
                  [{"text": "x", "repeat": 10 ** 15}],
                  [{"text": "xx", "repeat": limit}],
                  [{"text": "x", "repeat": limit}, {"text": "y"}],
                  [{"numbers": {"count": limit}}],
                  [{"numbers": {"count": 10 ** 12, "prefix": "k"}}]):
            with self.assertRaises(C.CaseError, msg=repr(r)):
                C.generate_input(r, "t")
        del at


class InCases(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_case_with_a_recipe_has_no_input_file_and_its_bytes(self):
        d = write_case(self.tmp, "r", [{"text": "ab", "repeat": 3}])
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["case.json"])
        self.assertEqual(C.load_own_case(d, self.tmp).input, b"ababab")

    def test_a_recipe_and_an_input_file_together_are_refused(self):
        d = write_case(self.tmp, "r", [{"text": "a"}], {"input": "input.yaml"})
        (d / "input.yaml").write_bytes(b"a")
        with self.assertRaises(C.CaseError):
            C.load_own_case(d, self.tmp)

    def test_without_either_the_case_is_refused(self):
        d = write_case(self.tmp, "r", None)
        with self.assertRaises(C.CaseError):
            C.load_own_case(d, self.tmp)

    def test_an_expected_event_list_can_come_from_a_recipe(self):
        recipe = [{"text": "+STR\n+DOC\n"}, {"numbers": {"count": 2, "prefix": "=VAL :", "suffix": "\n"}},
                  {"text": "-DOC\n-STR\n"}]
        exp = {"kind": "bounded", "time_seconds": 1, "memory_mb": 64, "error": "limit",
               "accept": {"kind": "events", "recipe": recipe}}
        d = write_case(self.tmp, "e", [{"text": "a"}], {"expect": exp})
        c = C.load_own_case(d, self.tmp)
        self.assertEqual(c.expect["accept"]["events"], ["+STR", "+DOC", "=VAL :0", "=VAL :1", "-DOC", "-STR"])
        (d / "x.events").write_text("+STR\n")
        both = {**exp, "accept": {"kind": "events", "recipe": recipe, "file": "x.events"}}
        d2 = write_case(self.tmp, "e2", [{"text": "a"}], {"expect": both})
        with self.assertRaises(C.CaseError):
            C.load_own_case(d2, self.tmp)


class Pins(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def digests(self):
        return C.own_case_digests(self.tmp), C.generated_input_digests(self.tmp)

    def test_changing_a_recipe_changes_the_pins(self):
        write_case(self.tmp, "r", [{"text": "ab", "repeat": 3}])
        before = self.digests()
        self.assertEqual(before[1]["safety/r"], {"bytes": 6, "sha256": hashlib.sha256(b"ababab").hexdigest()})
        case = self.tmp / "safety" / "r" / "case.json"
        original = json.loads(case.read_text())
        for recipe, same_bytes in (([{"text": "ab", "repeat": 4}], False), ([{"text": "ba", "repeat": 3}], False),
                                   ([{"text": "ab"}, {"text": "ab", "repeat": 2}], True)):
            case.write_text(json.dumps({**original, "input_recipe": recipe}))
            now = self.digests()
            # The recipe text is pinned with the case, the bytes it builds are pinned apart.
            self.assertNotEqual(now[0], before[0], recipe)
            self.assertEqual(now[1] == before[1], same_bytes, recipe)

    def test_only_generated_inputs_get_a_generated_pin(self):
        d = write_case(self.tmp, "stored", None, {"input": "input.yaml"})
        (d / "input.yaml").write_bytes(b"a")
        self.assertEqual(C.generated_input_digests(self.tmp), {})

    def test_the_pins_file_holds_the_digest_of_every_generated_input_in_the_kit(self):
        pinned = json.loads((support.ROOT / "tests" / "pins.json").read_bytes())["own"]["generated_inputs"]
        ids = {c.id[len("own:"):]: c for c in C.load_own()
               if "input_recipe" in json.loads((C.OWN_DIR / c.id[len("own:"):] / "case.json").read_bytes())}
        self.assertEqual(sorted(pinned), sorted(ids))
        self.assertGreaterEqual(len(ids), 3)
        for cid, c in ids.items():
            self.assertEqual(pinned[cid], {"bytes": len(c.input), "sha256": hashlib.sha256(c.input).hexdigest()})
            self.assertLessEqual(len(c.input), C.MAX_GENERATED_BYTES)


if __name__ == "__main__":
    unittest.main()
