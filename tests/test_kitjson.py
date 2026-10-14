"""Deep values: loading, validating, comparing and describing never recurse."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from oneyaml_kit import cases as C
from oneyaml_kit import kitjson as J

DEEP = 100_000


def nested_lists(n, leaf=None):
    v = leaf
    for _ in range(n):
        v = [v]
    return v


class Loading(unittest.TestCase):
    def test_ordinary_documents_load_the_same_way_with_both_parsers(self):
        docs = [
            '{"a": [1, 2.5, -0, 1e3, 12345678901234567890123], "b": {"c": null, "d": true, "e": false}}',
            '[]', '{}', '[[], {}, [[]], {"a": {}}]', '"a\\u00e9\\n\\"q\\""', '  [1 ,\n 2\t]  ', '-1.5e-3', '0',
            '[true, false, null]',
        ]
        for d in docs:
            self.assertEqual(J._loads_iterative(d), json.loads(d), d)

    def test_the_iterative_parser_rejects_what_json_rejects(self):
        for bad in ['', '[1,]', '{"a":1,}', '[1 2]', '{"a" 1}', 'tru', '[1]x', '{"a":}', '[', '{"a":1',
                    '{1:2}', '[01]', '--1', '"abc', ']', '{"a":1]', '[1}']:
            with self.assertRaises((ValueError, IndexError), msg=bad):
                J._loads_iterative(bad)

    def test_a_hundred_thousand_levels_deep(self):
        text = "[" * DEEP + "1" + "]" * DEEP
        with self.assertRaises(RecursionError):
            json.loads(text)  # the premise: the standard parser cannot do this
        v = J.loads(text)
        depth = 0
        while isinstance(v, list):
            v = v[0]
            depth += 1
        self.assertEqual((depth, v), (DEEP, 1))

    def test_deep_objects_and_bytes(self):
        text = '{"a":' * 5000 + "1" + "}" * 5000
        v = J.loads(text.encode())
        for _ in range(5000):
            v = v["a"]
        self.assertEqual(v, 1)

    def test_unterminated_deep_input_is_an_error_not_a_crash(self):
        with self.assertRaises(ValueError):
            J.loads("[" * DEEP)


class Comparing(unittest.TestCase):
    def test_deep_equal_values_are_equal(self):
        self.assertTrue(J.values_equal(nested_lists(DEEP, 1), nested_lists(DEEP, 1)))

    def test_a_difference_at_the_bottom_is_found_and_the_path_stays_short(self):
        d = J.value_diff(nested_lists(DEEP, 1), nested_lists(DEEP, 2))
        self.assertIn("expected 1, got 2", d)
        self.assertLess(len(d), 200)
        self.assertTrue(d.startswith("$..."))

    def test_a_difference_in_depth_is_not_equal(self):
        self.assertFalse(J.values_equal(nested_lists(DEEP, 1), nested_lists(DEEP + 1, 1)))
        self.assertIn("expected", J.value_diff(nested_lists(DEEP, 1), nested_lists(DEEP + 1, 1)))

    def test_tagged_and_map_forms_deep(self):
        v = {"$tagged": {"tag": "!t", "value": "x"}}
        for _ in range(DEEP):
            v = {"$tagged": {"tag": "!t", "value": v}}
        self.assertTrue(J.values_equal(v, v))
        self.assertTrue(J.values_equal(v, v, ignore_tags=True))
        m = {"$map": [["k", None]]}
        for _ in range(DEEP):
            m = {"$map": [["k", m]]}
        self.assertTrue(J.values_equal(m, m))

    def test_validation_is_iterative_and_still_strict(self):
        J.check_kit_value(nested_lists(DEEP, 1))
        bad = nested_lists(DEEP, {"$unknown": 1})
        with self.assertRaises(ValueError):
            J.check_kit_value(bad)
        self.assertFalse(J.values_equal(bad, bad))
        with self.assertRaises(ValueError):
            J.check_kit_value(nested_lists(DEEP, float("inf")))

    def test_mapping_order_and_equal_forms(self):
        self.assertTrue(J.values_equal({"a": 1, "b": [2]}, {"b": [2], "a": 1}))
        self.assertTrue(J.values_equal({"a": 1}, {"$map": [["a", 1]]}))
        self.assertTrue(J.values_equal({"$map": [[1, 2], [3, 4]]}, {"$map": [[3, 4], [1, 2]]}))
        self.assertFalse(J.values_equal([1, 2], [2, 1]))
        self.assertTrue(J.values_equal(0.0, -0.0))
        self.assertFalse(J.values_equal(1, 1.0))
        self.assertFalse(J.values_equal(1, True))

    def test_short_survives_values_too_deep_to_print(self):
        self.assertIn("too deeply", J.short(nested_lists(DEEP)))
        self.assertEqual(J.short([1, 2]), "[1, 2]")


class DuplicateKeys(unittest.TestCase):
    def test_a_repeated_key_is_refused_by_both_parsers(self):
        for text in ('{"a":1,"a":2}', '[{"x":{"k":1,"k":1}}]', '{"a":1,"b":2,"a":3}'):
            with self.assertRaises(ValueError, msg=text):
                J.loads(text)
            with self.assertRaises(ValueError, msg=text):
                J._loads_iterative(text)
        self.assertEqual(J.loads('{"a":{"a":1}}'), {"a": {"a": 1}})

    def test_a_repeated_key_deep_down_is_refused_too(self):
        text = '{"a":' * 5000 + '{"k":1,"k":2}' + '}' * 5000
        with self.assertRaises(ValueError):
            J.loads(text)


class DeepInline(unittest.TestCase):
    """A case, a correction or an upstream file may hold a value nested 100,000 levels deep."""

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, self.t)

    def deep(self):
        return "[" * DEEP + "null" + "]" * DEEP

    def test_an_own_case_with_an_inline_deep_value_loads(self):
        d = self.t / "cases" / "schema" / "deep"
        d.mkdir(parents=True)
        (d / "input.yaml").write_bytes(b"x\n")
        (d / "case.json").write_text(
            '{"format": "%s", "id": "schema/deep", "title": "t", "category": "schema", "origin": "spec", '
            '"refs": ["x"], "operation": {"type": "value"}, "expect": {"kind": "value", "value": [%s]}}'
            % (C.CASE_FORMAT, self.deep()))
        case = C.load_own_case(d, self.t / "cases")
        v = case.expect["value"]
        self.assertTrue(J.values_equal(v, [nested_lists(DEEP)]))

    def test_a_deep_correction_loads(self):
        p = self.t / "corrections.json"
        p.write_text('{"format": "%s", "entries": [{"case": "yts:X", "reason": "r", "citation": "c", '
                     '"value": [%s]}]}' % (C.CORRECTIONS_FORMAT, self.deep()))
        got = C.load_corrections(p)
        self.assertTrue(J.values_equal(got["yts:X"]["value"], [nested_lists(DEEP)]))

    def test_a_deep_upstream_stream_decodes_and_rewrites_dollar_keys(self):
        docs = C._decode_json_stream((self.deep() + "\n" + '{"$float": 1}\n').encode(), "t")
        self.assertEqual(len(docs), 2)
        self.assertTrue(J.values_equal(docs[0], nested_lists(DEEP)))
        self.assertEqual(C._to_kit_value(docs[1]), {"$map": [["$float", 1]]})
        deep_dollar = "[" * 5000 + '{"$map": 1}' + "]" * 5000
        out = C._to_kit_value(J.loads(deep_dollar))
        while isinstance(out, list):
            out = out[0]
        self.assertEqual(out, {"$map": [["$map", 1]]})


if __name__ == "__main__":
    unittest.main()
