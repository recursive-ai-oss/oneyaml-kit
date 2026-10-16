"""Properties of the seeded cases that the generic mutation tests do not check."""

import unittest

import support
from oneyaml_kit import cases as C
from oneyaml_kit import compare as K
from oneyaml_kit import kitjson

ALL = [c for c in C.load_all(include_slow=True) if c.suite == "own"]


class RoundTrip(unittest.TestCase):
    def test_a_round_trip_is_an_empty_patch_expecting_the_input(self):
        rt = [c for c in ALL if c.category == "roundtrip"]
        self.assertGreaterEqual(len(rt), 25)
        for c in rt:
            self.assertEqual((c.op, c.args.get("patch")), ("patch", []), c.id)
            self.assertEqual(c.expect["kind"], "bytes", c.id)
            self.assertEqual(c.expect["bytes"], c.input, c.id)

    def test_an_empty_patch_needs_a_patch_capability(self):
        c = next(c for c in ALL if c.category == "roundtrip")
        base, _ = K.required_caps(c, c.expect)
        self.assertFalse(K.satisfied(base, {"events", "value"}))
        self.assertTrue(K.satisfied(base, {"patch:replace"}))

    def test_round_trip_inputs_cover_every_encoding_and_line_break(self):
        data = {c.id: c.input for c in ALL if c.category == "roundtrip"}
        self.assertTrue(any(d.startswith(b"\xef\xbb\xbf") for d in data.values()))
        self.assertTrue(any(d.startswith(b"\xff\xfe\x00\x00") for d in data.values()))
        self.assertTrue(any(d.startswith(b"\xff\xfe") and not d.startswith(b"\xff\xfe\x00\x00")
                            for d in data.values()))
        self.assertTrue(any(b"\r\n" in d for d in data.values()))
        self.assertTrue(any(b"\r" in d.replace(b"\r\n", b"") for d in data.values()))


class Safety(unittest.TestCase):
    def test_every_safety_bound_is_explicit_and_modest(self):
        for c in ALL:
            if c.expect["kind"] == "bounded":
                self.assertLessEqual(c.expect["time_seconds"], 200, c.id)
                # A memory bound sits clear of what an honest reader needs to hold a large answer:
                # the largest generated case measures honest readers up to 1.5 GB.
                self.assertLessEqual(c.expect["memory_mb"], 2048, c.id)

    def test_seeded_cases_are_fast_tier(self):
        # Only the large generated inputs are slow; a case copied from another project is small.
        seeded = [c for c in ALL if c.provenance.get("source")]
        self.assertEqual([c.id for c in seeded if "slow" in c.tags], [])

    def test_every_large_generated_input_is_slow(self):
        # A big input in the fast tier costs every hand-over run; it belongs to the full gate.
        for c in ALL:
            if len(c.input) > 1_000_000:
                self.assertIn("slow", c.tags, c.id)

    def test_every_large_generated_result_is_slow(self):
        # The same for an expected result a recipe builds: it is megabytes to build and to compare.
        for c in ALL:
            acc = c.expect.get("accept") if c.expect["kind"] == "bounded" else c.expect
            if acc and acc["kind"] == "bytes" and len(acc["bytes"]) > 1_000_000:
                self.assertIn("slow", c.tags, c.id)


class Provenance(unittest.TestCase):
    def test_fuzz_cases_point_at_their_regression_file(self):
        fuzz = [c for c in ALL if c.origin == "fuzz"]
        self.assertGreaterEqual(len(fuzz), 3)
        for c in fuzz:
            self.assertTrue(c.provenance.get("reference"), c.id)

    def test_seeded_cases_name_the_revision_they_came_from(self):
        seeded = [c for c in ALL if c.provenance.get("source", "").endswith("oneyaml-rust")]
        self.assertGreaterEqual(len(seeded), 40)
        for c in seeded:
            self.assertEqual(c.provenance["revision"], "93e9e30", c.id)
            self.assertEqual(c.provenance["licence"], "MIT OR Apache-2.0", c.id)

    def test_inputs_stay_under_a_megabyte(self):
        root = support.ROOT / "suites" / "own"
        for p in root.rglob("*"):
            if p.is_file():
                self.assertLess(p.stat().st_size, 1_000_000, str(p))


class Values(unittest.TestCase):
    def test_value_files_hold_a_list_of_documents(self):
        for c in ALL:
            if c.expect["kind"] == "value":
                self.assertIsInstance(c.expect["value"], list, c.id)

    def test_expected_json_files_are_plain_json(self):
        for p in C.OWN_DIR.rglob("expected.json"):
            kitjson.loads(p.read_bytes())


class DeepShapes(unittest.TestCase):
    """The accepted results of the deep cases are rebuilt from their inputs by the grammar,
    so a typo in a stored file cannot hide until a second library answers."""

    def stored(self, case, name):
        d = support.ROOT / "suites" / "own" / "cases" / "safety" / case
        return (d / "input.yaml").read_bytes(), (d / name).read_bytes()

    @staticmethod
    def lines(items):
        return ("\n".join(items) + "\n").encode()

    def test_compact_block_sequences(self):
        for case in ("deep-block-sequences-3000", "deep-block-sequences-50000"):
            text, got = self.stored(case, "expected.events")
            n = text.count(b"- ")
            self.assertEqual(text, b"- " * n + b"a\n")
            want = ["+STR", "+DOC"] + ["+SEQ"] * n + ["=VAL :a"] + ["-SEQ"] * n + ["-DOC", "-STR"]
            self.assertEqual(got, self.lines(want), case)

    def test_explicit_keys(self):
        text, got = self.stored("deep-explicit-keys-2000", "expected.events")
        n = text.count(b"? ")
        self.assertEqual(text, b"? " * n + b"a\n")
        want = (["+STR", "+DOC"] + ["+MAP"] * n + ["=VAL :a"] + ["=VAL :", "-MAP"] * n
                + ["-DOC", "-STR"])
        self.assertEqual(got, self.lines(want))

    def test_flow_mappings(self):
        text, got = self.stored("deep-flow-mappings-3000", "expected.events")
        n = text.count(b"{")
        self.assertEqual(text, b"{a: " * n + b"x" + b"}" * n + b"\n")
        want = (["+STR", "+DOC"] + ["+MAP {}", "=VAL :a"] * n + ["=VAL :x"] + ["-MAP"] * n
                + ["-DOC", "-STR"])
        self.assertEqual(got, self.lines(want))

    def test_indented_mappings(self):
        text, got = self.stored("deep-indented-mappings-600", "expected.events")
        rows = text.decode().split("\n")[:-1]
        d = len(rows) - 1
        self.assertEqual(rows, [" " * i + "k:" for i in range(d)] + [" " * d + "end: 1"])
        want = (["+STR", "+DOC"] + ["+MAP", "=VAL :k"] * d
                + ["+MAP", "=VAL :end", "=VAL :1", "-MAP"] + ["-MAP"] * d + ["-DOC", "-STR"])
        self.assertEqual(got, self.lines(want))

    def test_hundred_thousand_deep_values(self):
        text, got = self.stored("deep-block-sequences-100000-value", "expected.json")
        n = text.count(b"- ")
        self.assertEqual(text, b"- " * n + b"a\n")
        self.assertEqual(got, b"[" + b"[" * n + b'"a"' + b"]" * n + b"]\n")
        text, got = self.stored("deep-flow-sequences-100000-value", "expected.json")
        n = text.count(b"[")
        self.assertEqual(text, b"[" * n + b"]" * n + b"\n")
        self.assertEqual(got, b"[" + b"[" * n + b"]" * n + b"]\n")


if __name__ == "__main__":
    unittest.main()
