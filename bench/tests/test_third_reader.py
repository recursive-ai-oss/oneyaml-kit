"""The third reader of the real-file workloads: it confirms every frozen value, and it fails when
a value is wrong. Needs node and npm; without them the tests fail rather than skip, because a
referee that is not running confirms nothing."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneyaml_bench import third_reader as T, workloads as W  # noqa: E402


def first_leaf_changed(v):
    """A copy of v with its first scalar replaced by a different one of the same kind of JSON."""
    if isinstance(v, dict):
        for k in v:
            new = first_leaf_changed(v[k])
            if new is not None:
                return {**v, k: new}
        return None
    if isinstance(v, list):
        for i, x in enumerate(v):
            new = first_leaf_changed(x)
            if new is not None:
                return v[:i] + [new] + v[i + 1:]
        return None
    if isinstance(v, bool):
        return not v
    if isinstance(v, (int, float)):
        return v + 1
    if isinstance(v, str):
        return v + "x"
    return None


class ThirdReader(unittest.TestCase):
    def test_every_real_workload_is_confirmed(self):
        result = T.check(W.REAL_DIR)
        self.assertGreaterEqual(len(result), 7)
        for name, diffs in result.items():
            self.assertEqual(diffs, [], f"{name}: the third reader reads a different value")

    def test_an_altered_expectation_is_caught_in_every_workload(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "real"
            for d in sorted(p for p in W.REAL_DIR.iterdir() if p.is_dir() and p.name != "LICENSES"):
                dest = copy / d.name
                dest.mkdir(parents=True)
                shutil.copyfile(d / "input.yaml", dest / "input.yaml")
                value = json.loads((d / "expected.json").read_text(encoding="utf-8"))
                altered = first_leaf_changed(value)
                self.assertIsNotNone(altered, d.name)
                (dest / "expected.json").write_text(json.dumps(altered), encoding="utf-8")
            result = T.check(copy)
            self.assertGreaterEqual(len(result), 7)
            for name, diffs in result.items():
                self.assertEqual(len(diffs), 1, f"{name}: one altered value must be one difference: {diffs}")

    def test_an_integer_and_a_float_are_different_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "real" / "numbers"
            d.mkdir(parents=True)
            (d / "input.yaml").write_text("a: 1\nb: 1.0\nc: 0x10\nd: 1e3\ne: '1'\nf: 007\n", encoding="utf-8")
            (d / "expected.json").write_text('{"a":1,"b":1.0,"c":16,"d":1000.0,"e":"1","f":7}', encoding="utf-8")
            self.assertEqual(T.check(d.parent), {"numbers": []})
            (d / "expected.json").write_text('{"a":1.0,"b":1.0,"c":16,"d":1000.0,"e":"1","f":7}', encoding="utf-8")
            self.assertEqual([p for p, *_ in T.check(d.parent)["numbers"]], ["/a"])
            # YAML 1.1 reads 007 as octal and `yes` as true; the core schema does not.
            (d / "input.yaml").write_text("a: yes\nb: 010\n", encoding="utf-8")
            (d / "expected.json").write_text('{"a":"yes","b":10}', encoding="utf-8")
            self.assertEqual(T.check(d.parent), {"numbers": []})

    def differences(self, text, expected):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "real" / "case"
            d.mkdir(parents=True)
            (d / "input.yaml").write_text(text, encoding="utf-8")
            (d / "expected.json").write_text(expected, encoding="utf-8")
            return T.check(d.parent)["case"]

    def test_what_a_loose_reading_would_hide_is_a_difference(self):
        # Each of these was once read without complaint: keys 1 and "1" collapsed to the last,
        # a tag the parser cannot resolve read as the text after it, binary data as a byte map.
        cases = {
            "keys of different types that print alike": ('1: a\n"1": b\n', '{"1":"b"}'),
            "a boolean key and a string key": ('true: a\n"true": b\n', '{"true":"b"}'),
            "a tag that cannot resolve": ("a: !!int abc\n", '{"a":"abc"}'),
            "a boolean tag on text": ("a: !!bool yes\n", '{"a":"yes"}'),
            "an unknown tag": ("a: !foo bar\n", '{"a":"bar"}'),
            "binary data": ("e: !!binary aGk=\n", '{"e":{"0":104.0,"1":105.0}}'),
            "a collection as a key": ("? [a, b]\n: c\n", '{"a,b":"c"}'),
            "negative zero": ("a: -0.0\n", '{"a":0.0}'),
        }
        for what, (text, expected) in cases.items():
            with self.subTest(what):
                self.assertTrue(self.differences(text, expected))

    def test_negative_zero_is_read_with_its_sign(self):
        self.assertEqual(self.differences("a: -0.0\nb: 0.0\n", '{"a":-0.0,"b":0.0}'), [])
        self.assertEqual([p for p, *_ in self.differences("a: 0.0\n", '{"a":-0.0}')], ["/a"])

    def test_plain_keys_of_other_types_are_read(self):
        # An unquoted 200 is an integer key; it prints as the text 200, as in the expectations.
        self.assertEqual(self.differences("200: ok\ntrue: t\nnull: n\n", '{"200":"ok","true":"t","null":"n"}'), [])

    def test_a_file_the_reader_refuses_is_a_difference(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "real" / "broken"
            d.mkdir(parents=True)
            (d / "input.yaml").write_text("a: [1, 2\n", encoding="utf-8")
            (d / "expected.json").write_text('{"a":[1,2]}', encoding="utf-8")
            (diff,) = T.check(d.parent)["broken"]
            self.assertIn("refused", diff[2])


if __name__ == "__main__":
    unittest.main()
