"""End to end: the oneyaml-rust adapter through the whole pipeline.

Needs a Rust toolchain and network access for the first build, so it only runs when
KIT_ADAPTER_TESTS=1 (the gate sets it).
"""

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

    def test_reference_case_a_passes_and_b_fails(self):
        a = self.by_id["own:edit/block-literal-keeps-style-with-newline"]
        b = self.by_id["own:edit/block-literal-strip-when-no-trailing-newline"]
        self.assertEqual((a["loose"], a["strict"]), ("pass", "pass"))
        self.assertEqual((b["loose"], b["strict"]), ("fail", "fail"))
        self.assertIn("|-", b["detail"])

    def test_repeated_keys_are_named_and_only_they_are(self):
        support.check_duplicate_key_category(self, "oneyaml-rust")

    def test_yaml_test_suite_scores(self):
        ev = [r for r in self.res["cases"] if r["suite"] == "yaml-test-suite" and r["op"] == "events"]
        self.assertEqual(len(ev), 402)
        self.assertEqual(sum(r["loose"] == "pass" for r in ev), 402)
        self.assertEqual(sum(r["strict"] == "pass" for r in ev), 393)
        self.assertEqual(sum(r.get("corrected", r)["strict"] == "pass" for r in ev), 400)

    def test_refusals_and_unsupported_operations_are_reported_as_such(self):
        self.assertEqual(self.by_id["own:edit/refuse-write-through-alias"]["loose"], "fail")
        self.assertEqual(self.by_id["own:edit/refuse-ambiguous-key"]["loose"], "fail")
        self.assertEqual(self.by_id["own:edit/atomic-failure"]["strict"], "pass")
        self.assertEqual(self.by_id["own:edit/failed-test-changes-nothing"]["loose"], "unsupported")
        self.assertEqual(self.by_id["own:edit/merge-patch"]["loose"], "unsupported")

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

    def test_adapter_made_behaviour_is_marked(self):
        self.assertEqual(self.res["adapter"]["emulates"], ["error", "unchanged"])
        marked = sorted(r["id"] for r in self.res["cases"] if r.get("emulated"))
        self.assertTrue({"own:edit/atomic-failure", "own:edit/pointer-without-leading-slash",
                         "own:edit/replace-missing-path", "own:edit/unknown-operation"} <= set(marked))
        # Only refusals are ever the adapter's: no edit that succeeded is credited to it.
        for r in self.res["cases"]:
            if r.get("emulated"):
                self.assertEqual((r["suite"], r["op"]), ("own", "patch"), r["id"])
                self.assertTrue(r["expect"] == "error" or r["loose"] == "fail", r["id"])
        # A failure that the library itself reports is not marked as the adapter's.
        self.assertEqual(self.by_id["own:edit/failed-test-changes-nothing"].get("emulated"), None)

    def test_report_mentions_the_scored_suite(self):
        self.assertIn("data-2022-01-17", report.render([self.res]))

    def test_an_integer_beyond_64_bits_is_declined_not_turned_into_a_float(self):
        from oneyaml_kit.protocol import SubprocessAdapter
        from oneyaml_kit.runner import load_manifest
        m = load_manifest("oneyaml-rust")
        ad = SubprocessAdapter(m["command"], cwd=m["_dir"], env=dict(os.environ), timeout=30)
        ad.start()
        try:
            self.assertEqual(ad.run("a", "dump", b"", {"value": [10**20]})["status"], "unsupported")
            self.assertEqual(ad.run("b", "dump", b"", {"value": [1e20, 2**64 - 1]})["status"], "ok")
        finally:
            ad.close()


if __name__ == "__main__":
    unittest.main()
