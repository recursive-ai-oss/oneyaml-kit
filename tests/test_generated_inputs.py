"""Inputs and expected results that a case describes with a recipe instead of storing."""

import hashlib
import json
import os
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


# A bytes result belongs to a patch operation; an empty patch is the way a round trip is asked for.
PATCH = {"operation": {"type": "patch", "patch": []}}


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

    def test_a_product_writes_every_combination_with_the_last_list_fastest(self):
        out = C.generate_input([
            {"product": {"prefix": "- &", "suffix": " 1\n", "choices": [["a", "b"], ["x", "yy", "z"]]}},
        ], "t")
        self.assertEqual(out, b"- &ax 1\n- &ayy 1\n- &az 1\n- &bx 1\n- &byy 1\n- &bz 1\n")

    def test_a_product_works_out_its_size_exactly_before_building(self):
        recipe = [{"product": {"prefix": "p", "choices": [["a", "bcd"]] * 10}}]
        self.assertEqual(len(C.generate_input(recipe, "t")), 1024 * 1 + 1024 * 10 * 2)
        too_big = [{"product": {"choices": [["a", "b"]] * 40}}]
        with self.assertRaises(C.CaseError):
            C.generate_input(too_big, "t")

    def test_a_product_of_few_long_lines_is_held_to_the_size_limit(self):
        limit = C.MAX_GENERATED_BYTES
        # Two lines of limit / 2 bytes each: exactly the limit, then one byte more.
        long = "x" * (limit // 2 - 3)
        product = {"product": {"prefix": "p", "suffix": "s", "choices": [[long], ["a", "b"]]}}
        # each line is "p" + long + "a" + "s": limit / 2 bytes
        at = C.generate_input([product], "t")
        self.assertEqual(len(at), limit)
        del at
        for r in ([product, {"text": "y"}],
                  [{"product": {"prefix": "pp", "suffix": "s", "choices": [[long], ["a", "b"]]}}],
                  [{"product": {"choices": [["x" * 1048576]] + [["a", "b"]] * 7}}]):
            with self.assertRaises(C.CaseError, msg=repr(r)[:80]):
                C.generate_input(r, "t")

    def test_a_product_with_runs_of_empty_lines_still_builds_every_combination(self):
        recipe = [{"product": {"choices": [["", "x"]] + [["", ""]] * 14}}]
        self.assertEqual(C.generate_input(recipe, "t"), b"x" * 16384)

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
            [{"product": {}}], [{"product": {"choices": []}}], [{"product": {"choices": [[]]}}],
            [{"product": {"choices": ["ab"]}}], [{"product": {"choices": [[1]]}}],
            [{"product": {"choices": [["a"]], "code": "x"}}], [{"product": {"choices": [["a"]], "prefix": 3}}],
            [{"product": 5}], [{"text": "a", "product": {"choices": [["a"]]}}],
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


class Memory(unittest.TestCase):
    def test_pieces_are_small_and_add_up_to_the_input(self):
        recipe = [{"text": "ab", "repeat": 3_000_000},
                  {"numbers": {"count": 200_000, "prefix": "k", "suffix": ": v, "}}]
        pieces = list(C.iter_input(recipe, "t"))
        self.assertGreater(len(pieces), 3)
        self.assertLessEqual(max(len(p) for p in pieces), 2 * 1024 * 1024)
        self.assertEqual(b"".join(pieces), C.generate_input(recipe, "t"))

    def test_building_numbers_stays_near_the_size_of_the_output(self):
        # Joining one bytes object per number before anything is written held about thirty times
        # the output at once.
        import tracemalloc
        recipe = [{"numbers": {"count": 1_000_000, "prefix": "=VAL :", "suffix": "\n=VAL :v\n"}}]
        tracemalloc.start()
        try:
            out = C.generate_input(recipe, "t")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 3 * len(out) + 8 * 1024 * 1024, (peak, len(out)))

    def test_the_digest_of_a_generated_input_is_taken_without_holding_it(self):
        import tracemalloc
        tmp = Path(tempfile.mkdtemp())
        try:
            write_case(tmp, "big", [{"text": "x", "repeat": 30_000_000}])
            tracemalloc.start()
            try:
                got = C.generated_input_digests(tmp)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(got["safety/big"]["sha256"], hashlib.sha256(b"x" * 30_000_000).hexdigest())
            self.assertLess(peak, 8 * 1024 * 1024, peak)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Slow(unittest.TestCase):
    def test_a_slow_case_is_not_built_unless_asked_for(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            write_case(tmp, "fast", [{"text": "a"}])
            write_case(tmp, "big", [{"text": "x", "repeat": 50_000_000}], {"tags": ["slow"]})
            self.assertEqual([c.id for c in C.load_own(tmp, include_slow=False)], ["own:safety/fast"])
            self.assertEqual(len(C.load_own(tmp)), 2)
            self.assertEqual(C.slow_ids(tmp), {"own:safety/big"})
            self.assertIsNone(C.load_own_case(tmp / "safety" / "big", tmp, skip_slow=True))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_kit_leaves_its_large_cases_out_of_the_fast_tier(self):
        slow = C.slow_ids()
        self.assertGreaterEqual(len(slow), 3)
        old = os.environ.pop("KIT_SLOW", None)
        try:
            self.assertFalse(slow & {c.id for c in C.load_all()})
            os.environ["KIT_SLOW"] = "1"
            self.assertTrue(slow <= {c.id for c in C.load_all()})
        finally:
            os.environ.pop("KIT_SLOW", None)
            if old is not None:
                os.environ["KIT_SLOW"] = old


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

    def test_an_expected_byte_string_can_come_from_a_recipe(self):
        recipe = [{"text": "a: "}, {"numbers": {"count": 3, "suffix": ","}}, {"text": "\n"}]
        exp = {"kind": "bounded", "time_seconds": 1, "memory_mb": 64, "error": "limit",
               "accept": {"kind": "bytes", "recipe": recipe}}
        d = write_case(self.tmp, "b", [{"text": "a"}], {"expect": exp, **PATCH})
        self.assertEqual(C.load_own_case(d, self.tmp).expect["accept"]["bytes"], b"a: 0,1,2,\n")
        # The same bytes in a file are the same expectation.
        (d / "x.yaml").write_bytes(b"a: 0,1,2,\n")
        stored = {**exp, "accept": {"kind": "bytes", "file": "x.yaml"}}
        d1 = write_case(self.tmp, "b1", [{"text": "a"}], {"expect": stored, **PATCH})
        (d1 / "x.yaml").write_bytes(b"a: 0,1,2,\n")
        self.assertEqual(C.load_own_case(d1, self.tmp).expect, C.load_own_case(d, self.tmp).expect)
        # A file and a recipe together, or neither, are refused; so is a recipe that is too large.
        for n, accept in enumerate(({"kind": "bytes", "recipe": recipe, "file": "x.yaml"}, {"kind": "bytes"},
                                    {"kind": "bytes", "recipe": [{"text": "x", "repeat": C.MAX_GENERATED_BYTES + 1}]},
                                    {"kind": "bytes", "recipe": [{"text": 5}]})):
            dd = write_case(self.tmp, "bad%d" % n, [{"text": "a"}],
                            {"expect": {**exp, "accept": accept}, **PATCH})
            (dd / "x.yaml").write_bytes(b"a")
            with self.assertRaises(C.CaseError, msg=repr(accept)):
                C.load_own_case(dd, self.tmp)

    def test_a_bad_result_recipe_is_reported_as_a_recipe_not_an_input(self):
        exp = {"kind": "bounded", "time_seconds": 1, "memory_mb": 64, "error": "limit",
               "accept": {"kind": "bytes", "recipe": []}}
        d = write_case(self.tmp, "m", [{"text": "a"}], {"expect": exp, **PATCH})
        with self.assertRaisesRegex(C.CaseError, r"recipe must be a list"):
            C.load_own_case(d, self.tmp)


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

    def test_a_result_recipe_has_a_pin_of_the_bytes_it_builds(self):
        exp = {"kind": "bounded", "time_seconds": 1, "memory_mb": 64, "error": "limit",
               "accept": {"kind": "bytes", "recipe": [{"text": "ab", "repeat": 3}]}}
        d = write_case(self.tmp, "r", [{"text": "a"}], {"expect": exp, **PATCH})
        self.assertEqual(C.generated_result_digests(self.tmp),
                         {"safety/r": {"bytes": 6, "sha256": hashlib.sha256(b"ababab").hexdigest()}})
        case = d / "case.json"
        original = json.loads(case.read_text())
        before = self.digests()
        for recipe, same_bytes in (([{"text": "ab", "repeat": 4}], False), ([{"text": "ba", "repeat": 3}], False),
                                   ([{"text": "ab"}, {"text": "ab", "repeat": 2}], True)):
            changed = json.loads(json.dumps(original))
            changed["expect"]["accept"]["recipe"] = recipe
            case.write_text(json.dumps(changed))
            now = C.generated_result_digests(self.tmp)
            self.assertEqual(now["safety/r"] == {"bytes": 6, "sha256": hashlib.sha256(b"ababab").hexdigest()},
                             same_bytes, recipe)
            self.assertNotEqual(self.digests()[0], before[0], recipe)  # the recipe text is pinned with the case

    def test_the_pins_file_holds_the_digest_of_every_generated_result_in_the_kit(self):
        pinned = json.loads((support.ROOT / "tests" / "pins.json").read_bytes())["own"]["generated_results"]
        self.assertEqual(sorted(pinned), sorted(C.generated_result_digests()))
        self.assertIn("safety/patch-many-large-appends-into-a-created-flow-sequence", pinned)
        self.assertGreaterEqual(len(pinned), 4)

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
