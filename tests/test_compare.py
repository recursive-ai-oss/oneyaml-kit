import unittest

import support
from oneyaml_kit import compare as K
from oneyaml_kit.cases import Case, b64

CAPS = set(support.ALL_CAPS)


def case(op="events", expect=None, data=b"x\n", args=None):
    return Case(id="t", suite="own", category="parse", title="t", origin="bug", op=op,
                input=data, expect=expect, args=args or {})


def err(**kw):
    e = {"kind": "error", "category": "any", "line": None, "column": None, "events": None, "unchanged": False}
    e.update(kw)
    return e


def ev(case_, resp, caps=CAPS):
    r = K.evaluate(case_, case_.expect, resp, caps)
    return r["loose"], r["strict"]


class Values(unittest.TestCase):
    def test_types_are_strict(self):
        self.assertFalse(K.values_equal(1, 1.0))
        self.assertFalse(K.values_equal(1, True))
        self.assertFalse(K.values_equal(0, False))
        self.assertFalse(K.values_equal("1", 1))
        self.assertFalse(K.values_equal(None, ""))
        self.assertTrue(K.values_equal(1.5, 1.5))

    def test_escape_forms_cannot_collide_with_real_data(self):
        # YAML `$float: inf` is the map form; it is not positive infinity.
        yaml_mapping = {"$map": [["$float", "inf"]]}
        infinity = {"$float": "inf"}
        self.assertFalse(K.values_equal([yaml_mapping], [infinity]))
        self.assertFalse(K.values_equal([infinity], [float("inf")]))
        # An object that uses a reserved key as data is malformed, never equal to anything.
        self.assertFalse(K.values_equal([{"$float": "inf", "a": 1}], [{"$float": "inf", "a": 1}]))
        self.assertFalse(K.values_equal([{"$map": [[1, 2]], "x": 1}], [{"$map": [[1, 2]], "x": 1}]))
        self.assertFalse(K.values_equal([{"$unknown": 1}], [{"$unknown": 1}]))
        self.assertTrue(K.values_equal([{"$map": [["$map", [[1, 2]]]]}], [{"$map": [["$map", [[1, 2]]]]}]))
        self.assertFalse(K.values_equal([{"$map": [["$map", [[1, 2]]]]}], [{"$map": [[1, 2]]}]))

    def test_tags_are_part_of_the_value_unless_a_case_says_otherwise(self):
        tagged = {"$tagged": {"tag": "!x", "value": "a"}}
        self.assertFalse(K.values_equal(tagged, "a"))
        self.assertFalse(K.values_equal(tagged, {"$tagged": {"tag": "!y", "value": "a"}}))
        self.assertTrue(K.values_equal(tagged, dict(tagged)))
        self.assertTrue(K.values_equal(tagged, "a", ignore_tags=True))
        self.assertTrue(K.values_equal([{"k": tagged}], [{"k": "a"}], ignore_tags=True))
        self.assertFalse(K.values_equal(tagged, "b", ignore_tags=True))

    def test_exact_number_typing_has_no_loophole(self):
        self.assertFalse(K.values_equal([450], [450.0]))
        self.assertFalse(K.values_equal([450], [450.0], ignore_tags=True))

    def test_mappings_are_unordered_sequences_are_not(self):
        self.assertTrue(K.values_equal({"a": 1, "b": 2}, {"b": 2, "a": 1}))
        self.assertFalse(K.values_equal([1, 2], [2, 1]))

    def test_map_form_equals_object_form(self):
        self.assertTrue(K.values_equal({"a": 1}, {"$map": [["a", 1]]}))
        self.assertFalse(K.values_equal({"a": 1}, {"$map": [[1, 1]]}))

    def test_special_floats(self):
        nan = {"$float": "nan"}
        self.assertTrue(K.values_equal([nan], [{"$float": "nan"}]))
        self.assertFalse(K.values_equal({"$float": "inf"}, {"$float": "-inf"}))
        self.assertFalse(K.values_equal({"$float": "inf"}, "inf"))

    def test_non_kit_values_are_unequal_not_crashes(self):
        self.assertFalse(K.values_equal({1, 2}, {1, 2}))

    def test_diff_names_the_path(self):
        d = K.value_diff([{"a": [1, {"b": 2}]}], [{"a": [1, {"b": 3}]}])
        self.assertEqual(d, "$[0].a[1].b: expected 2, got 3")


class Events(unittest.TestCase):
    def test_exact_match_passes_and_any_difference_fails(self):
        c = case(expect={"kind": "events", "events": ["+STR", "-STR"]})
        self.assertEqual(ev(c, {"status": "ok", "events": ["+STR", "-STR"]}), ("pass", "pass"))
        self.assertEqual(ev(c, {"status": "ok", "events": ["+STR"]}), ("fail", "fail"))
        self.assertEqual(ev(c, {"status": "ok", "events": ["+STR", "-STR", "x"]}), ("fail", "fail"))
        self.assertEqual(ev(c, {"status": "ok", "events": ["+STR", "-STR "]}), ("fail", "fail"))
        self.assertEqual(ev(c, {"status": "error", "error": {"category": "syntax"}}), ("fail", "fail"))

    def test_error_cases_loose_and_strict(self):
        c = case(expect=err(events=["+STR", "+DOC"]))
        good = {"status": "error", "error": {"category": "syntax"}, "events": ["+STR", "+DOC"]}
        self.assertEqual(ev(c, good), ("pass", "pass"))
        # Loose only needs an error; strict also needs the recorded prefix.
        bad = dict(good, events=["+STR"])
        self.assertEqual(ev(c, bad), ("pass", "fail"))
        self.assertEqual(ev(c, {"status": "ok", "events": []}), ("fail", "fail"))

    def test_category_and_position_are_checked_in_strict_only(self):
        c = case(expect=err(category="syntax", line=2, column=3))
        r = {"status": "error", "error": {"category": "syntax", "line": 2, "column": 3}}
        self.assertEqual(ev(c, r), ("pass", "pass"))
        for category, line, column in ("limit", 2, 3), ("syntax", 1, 3), ("syntax", 2, 4):
            e = {"category": category, "line": line, "column": column}
            self.assertEqual(ev(c, {"status": "error", "error": e}), ("pass", "fail"), e)
        self.assertEqual(ev(c, {"status": "error", "error": {"category": "syntax"}}), ("pass", "fail"))

    def test_line_only_expectation_ignores_the_column(self):
        c = case(expect=err(line=2))
        self.assertEqual(ev(c, {"status": "error", "error": {"line": 2, "column": 9}}), ("pass", "pass"))
        self.assertEqual(ev(c, {"status": "error", "error": {"line": 3}}), ("pass", "fail"))

    def test_missing_capability_makes_strict_unsupported_not_pass(self):
        c = case(expect=err(category="syntax", line=2, column=3))
        r = {"status": "error", "error": {"category": "syntax", "line": 2, "column": 3}}
        self.assertEqual(ev(c, r, caps={"events"}), ("pass", "unsupported"))
        # A stated check that fails still fails, whatever else is missing.
        late = {"status": "error", "error": {"category": "limit"}, "events": []}
        c2 = case(expect=err(category="syntax", events=["+STR"]))
        self.assertEqual(ev(c2, late, caps={"events", "error-category"}), ("pass", "fail"))

    def test_crash_and_timeout_fail(self):
        c = case(expect=err())
        self.assertEqual(ev(c, {"status": "crash", "detail": "x"}), ("fail", "fail"))
        self.assertEqual(ev(c, {"status": "timeout", "detail": "x"}), ("fail", "fail"))
        self.assertEqual(ev(c, {"status": "weird"}), ("fail", "fail"))

    def test_bounded_outcomes(self):
        accept = {"kind": "events", "events": ["+STR", "-STR"]}
        b = {"kind": "bounded", "time_seconds": 2, "memory_mb": 100, "accept": accept, "category": "limit"}
        c = case(expect=b)
        m = {"seconds": 0.5, "peak_mb": 10.0}
        ok = {"status": "ok", "events": ["+STR", "-STR"], "_measure": m}
        self.assertEqual(ev(c, ok), ("pass", "pass"))
        # Garbage is not an accepted outcome, whatever else is true.
        self.assertEqual(ev(c, dict(ok, events=["+STR"])), ("fail", "fail"))
        self.assertEqual(ev(c, dict(ok, events=[])), ("fail", "fail"))
        lim = {"status": "error", "error": {"category": "limit"}, "_measure": m}
        self.assertEqual(ev(c, lim), ("pass", "pass"))
        # Any error passes loose; the named category is a strict matter.
        self.assertEqual(ev(c, {"status": "error", "error": {"category": "syntax"}, "_measure": m}), ("pass", "fail"))
        self.assertEqual(ev(c, lim, caps={"events"}), ("pass", "unsupported"))
        # The bounds are part of the expectation.
        self.assertEqual(ev(c, dict(ok, _measure={"seconds": 2.5, "peak_mb": 1})), ("fail", "fail"))
        self.assertEqual(ev(c, dict(ok, _measure={"seconds": 0.1, "peak_mb": 101})), ("fail", "fail"))
        self.assertEqual(ev(c, dict(ok, _measure={"seconds": 0.1, "peak_mb": None})), ("pass", "unsupported"))
        for st in ("crash", "timeout", "memory"):
            self.assertEqual(ev(c, {"status": st}), ("fail", "fail"))
        only_error = case(expect=dict(b, accept=None))
        self.assertEqual(ev(only_error, ok), ("fail", "fail"))
        only_result = case(expect=dict(b, category=None))
        self.assertEqual(ev(only_result, lim), ("fail", "fail"))
        self.assertEqual(ev(only_result, ok), ("pass", "pass"))

    def test_summary_records_what_came_back(self):
        g = K.summarize({"status": "error", "error": {"category": "syntax", "line": 2, "column": 4, "message": "m"},
                         "events": ["+STR"]})
        self.assertEqual(g, {"status": "error", "category": "syntax", "line": 2, "column": 4, "message": "m",
                             "events": 1})

    def test_an_operation_name_outside_the_rfc_needs_only_some_patch_capability(self):
        patch = [{"op": "frobnicate", "path": "/a"}]
        c = case("patch", err(category="patch-invalid", unchanged=True), args={"patch": patch})
        base, strict = K.required_caps(c, c.expect)
        self.assertEqual(base, {K.ANY_PATCH})
        self.assertEqual(strict, {"error-category"})
        self.assertTrue(K.satisfied(base, {"events", "patch:replace"}))
        self.assertFalse(K.satisfied(base, {"events", "value"}))


class Patches(unittest.TestCase):
    def pc(self, expect, ops=None):
        ops = ops or [{"op": "replace", "path": "/a", "value": 2}]
        return case("patch", expect, data=b"a: 1\n", args={"patch": ops})

    def test_every_byte_counts(self):
        c = self.pc({"kind": "bytes", "bytes": b"a: 2\n"})
        self.assertEqual(ev(c, {"status": "ok", "output": b64(b"a: 2\n")}), ("pass", "pass"))
        self.assertEqual(ev(c, {"status": "ok", "output": b64(b"a: 2")}), ("fail", "fail"))
        self.assertEqual(ev(c, {"status": "ok", "output": b64(b"a: 2\r\n")}), ("fail", "fail"))
        self.assertEqual(ev(c, {"status": "ok", "output": b64(b"\xef\xbb\xbfa: 2\n")}), ("fail", "fail"))
        self.assertEqual(ev(c, {"status": "ok"}), ("fail", "fail"))
        self.assertEqual(ev(c, {"status": "ok", "output": "!!!"}), ("fail", "fail"))

    def test_refusal_must_leave_the_document_unchanged(self):
        c = self.pc(err(category="patch-path", unchanged=True))
        ok = {"status": "error", "error": {"category": "patch-path"}, "output": b64(b"a: 1\n")}
        self.assertEqual(ev(c, ok), ("pass", "pass"))
        changed = dict(ok, output=b64(b"a: 2\n"))
        self.assertEqual(ev(c, changed), ("pass", "fail"))
        self.assertEqual(ev(c, {"status": "error", "error": {"category": "patch-path"}}), ("pass", "fail"))
        self.assertEqual(ev(c, {"status": "ok", "output": b64(b"a: 2\n")}), ("fail", "fail"))

    def test_required_capabilities_follow_the_patch(self):
        ops = [{"op": "add", "path": "/b", "value": 1}, {"op": "test", "path": "/a", "value": 1}]
        c = self.pc({"kind": "bytes", "bytes": b""}, ops=ops)
        base, strict = K.required_caps(c, c.expect)
        self.assertEqual(base, {"patch:add", "patch:test"})
        self.assertEqual(strict, set())
        m = case("patch", {"kind": "bytes", "bytes": b""}, args={"format": "merge-patch", "patch": {}})
        self.assertEqual(K.required_caps(m, m.expect)[0], {"patch:merge"})
        p = case("events", err(category="syntax", line=1, column=1), args={"profile": "yaml-1.1"})
        self.assertEqual(K.required_caps(p, p.expect),
                         ({"events|outcome:events", "profile:yaml-1.1"}, {"error-category", "error-position"}))


if __name__ == "__main__":
    unittest.main()
