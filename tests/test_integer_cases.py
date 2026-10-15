"""The cases that ask for exact integers past 64 bits, or a refusal with a named category."""

import unittest

import support
from oneyaml_kit import cases as C
from oneyaml_kit import compare as K

IDS = ("own:schema/decimal-integers-beyond-64-bits-are-exact-or-refused",
       "own:schema/hexadecimal-and-octal-integers-beyond-64-bits-are-exact-or-refused",
       "own:safety/integer-of-4300-digits-is-exact-or-refused")
CAPS = set(support.ALL_CAPS)
MEASURE = {"seconds": 0.1, "peak_mb": 10.0}


def load():
    by_id = {c.id: c for c in C.load_own()}
    return [by_id[i] for i in IDS]


def verdict(case, resp):
    r = K.evaluate(case, case.expect, dict(resp, _measure=MEASURE), CAPS)
    return r["loose"], r["strict"]


def ints(v):
    """Every integer in a kit value, in order."""
    if isinstance(v, bool):
        return []
    if isinstance(v, int):
        return [v]
    if isinstance(v, list):
        return [i for x in v for i in ints(x)]
    if isinstance(v, dict):
        return [i for x in v.values() for i in ints(x)]
    return []


class IntegerCases(unittest.TestCase):
    def test_the_expectation_is_the_exact_integer_or_a_limit_error(self):
        for c in load():
            e = c.expect
            self.assertEqual((e["kind"], e["category"]), ("bounded", ["limit"]), c.id)
            self.assertEqual(e["accept"]["kind"], "value", c.id)
            self.assertTrue(all(abs(i) >= 2 ** 63 for i in ints(e["accept"]["value"])), c.id)

    def test_the_values_are_the_ones_the_core_schema_gives(self):
        dec, hexoct, long = load()
        # [-+]?[0-9]+, 0o[0-7]+ and 0x[0-9a-fA-F]+ (section 10.3.2) have no upper bound.
        self.assertEqual(ints(dec.expect["accept"]["value"]),
                         [2 ** 64, -(2 ** 63) - 1, 123456789012345678901234567890, 10 ** 99])
        self.assertEqual(ints(hexoct.expect["accept"]["value"]), [2 ** 72 - 1, 2 ** 64, 2 ** 66 - 1])
        digits = str(ints(long.expect["accept"]["value"])[0])
        self.assertEqual(len(digits), 4300)
        self.assertEqual(long.input.decode(), "a: " + digits + "\n")

    def test_exact_integers_and_a_limit_error_pass_and_anything_else_fails(self):
        for c in load():
            want = c.expect["accept"]["value"]
            self.assertEqual(verdict(c, {"status": "ok", "value": want}), ("pass", "pass"), c.id)
            self.assertEqual(verdict(c, {"status": "error", "error": {"category": "limit"}}), ("pass", "pass"), c.id)
            # A refusal of another kind is an error, but not the named one.
            self.assertEqual(verdict(c, {"status": "error", "error": {"category": "syntax"}}), ("pass", "fail"), c.id)
            # Rounded to a double, kept as text, wrapped to 64 bits, or one digit off: all different data.
            rounded = _map(want, lambda i: float(i % 10 ** 300))
            self.assertEqual(verdict(c, {"status": "ok", "value": rounded}), ("fail", "fail"), c.id)
            self.assertEqual(verdict(c, {"status": "ok", "value": _map(want, str)}), ("fail", "fail"), c.id)
            self.assertEqual(verdict(c, {"status": "ok", "value": _map(want, lambda i: i % 2 ** 64)}),
                             ("fail", "fail"), c.id)
            self.assertEqual(verdict(c, {"status": "ok", "value": _map(want, lambda i: i + 1)}),
                             ("fail", "fail"), c.id)

    def test_the_oracle_adapter_passes_them(self):
        cases = load()
        oracle = support.OracleAdapter(cases)
        for c in cases:
            r = oracle.run(c.id, "value", c.input, c.args, limits={})
            self.assertEqual(verdict(c, r), ("pass", "pass"), c.id)


def _map(v, fn):
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return fn(v)
    if isinstance(v, list):
        return [_map(x, fn) for x in v]
    if isinstance(v, dict):
        return {k: _map(x, fn) for k, x in v.items()}
    return v


if __name__ == "__main__":
    unittest.main()
