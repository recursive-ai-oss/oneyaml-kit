"""Properties of the seeded cases that the generic mutation tests do not check."""

import json
import unittest

import support
from oneyaml_kit import cases as C
from oneyaml_kit import compare as K

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
                self.assertLessEqual(c.expect["time_seconds"], 20, c.id)
                self.assertLessEqual(c.expect["memory_mb"], 1024, c.id)

    def test_seeded_cases_are_fast_tier(self):
        self.assertEqual([c.id for c in ALL if "slow" in c.tags], [])


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
            json.loads(p.read_bytes())


if __name__ == "__main__":
    unittest.main()
