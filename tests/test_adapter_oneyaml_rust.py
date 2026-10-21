"""End to end: the oneyaml-rust adapter through the whole pipeline.

Needs a Rust toolchain and network access for the first build, so it only runs when
KIT_ADAPTER_TESTS=1 (the gate sets it).
"""

import base64
import os
import unittest

import support
from oneyaml_kit import cases as C
from oneyaml_kit import report
from oneyaml_kit.runner import run_adapter, validate_result


@unittest.skipUnless(os.environ.get("KIT_ADAPTER_TESTS") == "1", "set KIT_ADAPTER_TESTS=1 to build and run adapters")
class OneyamlRust(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = run_adapter("oneyaml-rust", C.load_all(), complete=True)
        cls.by_id = {r["id"]: r for r in cls.res["cases"]}

    def test_result_is_well_formed_and_deviations_hold(self):
        self.assertEqual(validate_result(self.res), [])
        d = self.res["deviations"]
        self.assertEqual((d["unlisted"], d["stale"], d["unknown"]), ([], [], []))

    def test_reference_edit_cases_pass(self):
        for cid in ("own:edit/block-literal-keeps-style-with-newline",
                    "own:edit/block-literal-strip-when-no-trailing-newline"):
            r = self.by_id[cid]
            self.assertEqual((r["loose"], r["strict"]), ("pass", "pass"), cid)

    def test_repeated_keys_are_named_and_only_they_are(self):
        support.check_duplicate_key_category(self, "oneyaml-rust")

    def test_yaml_test_suite_scores(self):
        ev = [r for r in self.res["cases"] if r["suite"] == "yaml-test-suite" and r["op"] == "events"]
        self.assertEqual(len(ev), 402)
        self.assertEqual(sum(r["loose"] == "pass" for r in ev), 402)
        # Two cases whose upstream events end the document before an error: the engine follows the
        # corrected expectation, which the strict primary score does not.
        self.assertEqual(sum(r["strict"] == "pass" for r in ev), 391)
        self.assertEqual(sum(r.get("corrected", r)["strict"] == "pass" for r in ev), 402)

    def test_the_library_applies_every_patch_operation_and_makes_its_own_refusals(self):
        for cid in ("own:edit/refuse-write-through-alias", "own:edit/refuse-ambiguous-key",
                    "own:edit/atomic-failure", "own:edit/failed-test-changes-nothing",
                    "own:edit/merge-patch"):
            r = self.by_id[cid]
            self.assertEqual((r["loose"], r["strict"]), ("pass", "pass"), cid)

    def test_safety_cases_record_the_outcome_and_the_measurements(self):
        for cid in ("own:safety/deeply-nested-flow-sequences", "own:safety/alias-expansion-bomb"):
            r = self.by_id[cid]
            self.assertEqual(r["strict"], "pass", cid)
            self.assertEqual(r["got"]["category"], "limit", cid)
            self.assertLess(r["measured"]["seconds"], r["measured"]["time_bound"])
        self.assertEqual(self.res["run"]["adapter_restarts"], 0)

    def test_value_typing_is_exact_and_the_one_upstream_error_is_corrected(self):
        v = [r for r in self.res["cases"] if r["suite"] == "yaml-test-suite" and r["op"] == "value"]
        self.assertEqual(len(v), 279)
        self.assertEqual([r["id"] for r in v if r["loose"] != "pass"], ["yts:UGM3@value"])
        self.assertEqual(self.by_id["yts:UGM3@value"]["corrected"]["loose"], "pass")

    def test_the_hello_names_the_pinned_revision(self):
        # The adapter builds from a repository revision, not a release, so the version says which.
        self.assertEqual(self.res["adapter"]["version"], "1.1.0+3ea4ac94d92a")

    def test_nothing_is_emulated(self):
        # The library does the patching, refusals and all-or-nothing included.
        self.assertEqual(self.res["adapter"]["emulates"], [])
        self.assertEqual([r["id"] for r in self.res["cases"] if r.get("emulated")], [])

    def test_report_mentions_the_scored_suite(self):
        self.assertIn("data-2022-01-17", report.render([self.res]))

    def test_a_byte_string_is_declined_not_written_as_a_mapping(self):
        r = self.by_id["own:emit/binary-values-are-tagged-base64"]
        self.assertEqual((r["loose"], r["strict"]), ("unsupported", "unsupported"))
        # serialize_bytes in the pinned oneyaml writes a sequence of integers, as serde_yaml does.
        self.assertIn("list of integers", r["detail"])

    def test_an_integer_beyond_64_bits_is_written_exactly_not_turned_into_a_float(self):
        from oneyaml_kit.protocol import SubprocessAdapter
        from oneyaml_kit.runner import load_manifest
        m = load_manifest("oneyaml-rust")
        ad = SubprocessAdapter(m["command"], cwd=m["_dir"], env=dict(os.environ), timeout=30)
        ad.start()
        try:
            # The library's own wide integer type holds it, so it is written in full.
            r = ad.run("a", "dump", b"", {"value": [10**20, -10**30]})
            self.assertEqual(r["status"], "ok")
            text = base64.b64decode(r["output"])
            self.assertEqual(text, b"- 100000000000000000000\n- -1000000000000000000000000000000\n")
            self.assertEqual(ad.run("b", "dump", b"", {"value": [1e20, 2**64 - 1]})["status"], "ok")
        finally:
            ad.close()

    def test_decode_errors_carry_the_library_position(self):
        from oneyaml_kit.protocol import SubprocessAdapter
        from oneyaml_kit.runner import load_manifest
        m = load_manifest("oneyaml-rust")
        ad = SubprocessAdapter(m["command"], cwd=m["_dir"], env=dict(os.environ), timeout=30)
        ad.start()
        try:
            bom = b"\xef\xbb\xbf"
            utf16 = b"\xff\xfe" + "a\r\nb".encode("utf-16-le")[:]
            probes = [
                (b"a: 1\n\xffb", (2, 1)),
                (b"a: 1\r\nb: \xc3(", (2, 4)),
                (b"a\rb \x80", (2, 3)),
                (bom + b"ab\xff", (1, 3)),
                (b"a: \xe2\x82", (1, 4)),
                (utf16 + b"\x00\xdc", (2, 2)),
            ]
            for i, (data, want) in enumerate(probes):
                r = ad.run(f"p{i}", "events", data, {})
                self.assertEqual(r["status"], "error", data)
                e = r["error"]
                self.assertEqual((e["category"], e["line"], e["column"]), ("encoding", *want), data)
        finally:
            ad.close()


if __name__ == "__main__":
    unittest.main()
