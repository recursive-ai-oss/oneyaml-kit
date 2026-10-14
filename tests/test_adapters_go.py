"""The Go adapters: pins, honest declarations and the numbers they produce.

The static checks always run. Building and running the adapters needs a Go toolchain and
a populated module cache, so that part only runs when KIT_ADAPTER_TESTS=1 (the gate sets it).
"""

import os
import re
import subprocess
import unittest

import support
from oneyaml_kit import cases as C
from oneyaml_kit import report, runner
from oneyaml_kit.runner import run_adapter, validate_result, verify_deviations

ADAPTERS = support.ROOT / "adapters"
GO = sorted(p.name for p in ADAPTERS.iterdir() if (p / "go.mod").is_file() and (p / "adapter.json").is_file())

# What each library can do, from the library's own API. Declaring more would be a claim
# the kit then judges; declaring less hides results.
CAPS = {
    "go-yaml-v2": {"events", "value"},
    "go-yaml-v3": {"events", "value", "patch:add", "patch:remove", "patch:replace"},
    "go-yaml-v4": {"events", "value", "error-category", "error-position",
                   "patch:add", "patch:remove", "patch:replace"},
    "goccy-go-yaml": {"events", "value", "error-category", "error-position", "patch:replace"},
    "sigs-k8s-yaml": {"events", "value"},
    "palantir-yamlpatch-v3": {"patch:add", "patch:remove", "patch:replace", "patch:move",
                              "patch:copy", "patch:test"},
    "palantir-yamlpatch-goccy": {"patch:add", "patch:remove", "patch:replace", "patch:move",
                                 "patch:copy", "patch:test"},
}

# yaml-test-suite results for the parsers, to be read next to the survey's own probe of
# the same libraries (valid inputs matching their JSON, invalid inputs rejected).
# Pinned so that a change in an adapter or a library shows up here.
VALUE_PASSES = {"go-yaml-v2": 223, "go-yaml-v3": 223, "go-yaml-v4": 232, "goccy-go-yaml": 260,
                "sigs-k8s-yaml": 209}
REJECTED = {"go-yaml-v2": 79, "go-yaml-v3": 79, "go-yaml-v4": 80, "goccy-go-yaml": 84,
            "sigs-k8s-yaml": 72}

CASE_A = "own:edit/block-literal-keeps-style-with-newline"
CASE_B = "own:edit/block-literal-strip-when-no-trailing-newline"


class Static(unittest.TestCase):
    def test_the_expected_adapters_exist(self):
        self.assertEqual(GO, sorted(CAPS))

    def test_every_go_adapter_is_pinned_and_registered(self):
        for name in GO:
            d = ADAPTERS / name
            mod = (d / "go.mod").read_text()
            self.assertTrue((d / "go.sum").is_file(), name)
            self.assertNotIn("latest", mod, name)
            self.assertEqual(re.findall(r"^replace (\S+) => (\S+)$", mod, re.M),
                             [("example.invalid/oneyaml-kit/gokit", "../gokit")], name)
            m = runner.load_manifest(name)
            self.assertEqual(m["name"], name)
            self.assertEqual(m["language"], "go")
            self.assertTrue((d / m["deviations"]).is_file(), name)

    def test_go_build_output_stays_out_of_the_repository(self):
        r = subprocess.run(["git", "check-ignore", "-q", "adapters/gokit/.bin/go-yaml-v3"], cwd=support.ROOT)
        self.assertEqual(r.returncode, 0)


@unittest.skipUnless(os.environ.get("KIT_ADAPTER_TESTS") == "1", "set KIT_ADAPTER_TESTS=1 to build and run adapters")
class Run(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cases = C.load_all()
        cls.res = {n: run_adapter(n, cases, complete=True) for n in GO}
        cls.by = {n: {r["id"]: r for r in res["cases"]} for n, res in cls.res.items()}

    def test_results_are_well_formed_and_the_deviation_lists_hold(self):
        for n, res in self.res.items():
            self.assertEqual(validate_result(res), [], n)
            d = res["deviations"]
            self.assertEqual((d["unlisted"], d["stale"], d["unknown"]), ([], [], []), n)
            self.assertGreater(d["listed"], 0, n)

    def test_declared_capabilities_and_pinned_versions(self):
        for n, res in self.res.items():
            a = res["adapter"]
            self.assertEqual(set(a["capabilities"]), CAPS[n], n)
            mod, ver = a["info"]["module"].rsplit(" ", 1)
            self.assertEqual(a["version"], ver, n)
            gomod = (ADAPTERS / n / "go.mod").read_text()
            # The library the adapter reports is the library the module requires.
            if n.startswith("palantir"):
                mod = "github.com/palantir/pkg/yamlpatch"
            self.assertRegex(gomod, rf"(?m)^\s*{re.escape(mod)} {re.escape(ver)}$", n)

    def test_the_parsers_score_what_their_libraries_score(self):
        for n, want in VALUE_PASSES.items():
            v = [r for r in self.res[n]["cases"] if r["suite"] == "yaml-test-suite" and r["op"] == "value"]
            self.assertEqual(len(v), 279)
            self.assertEqual(sum(r["loose"] == "pass" for r in v), want, n)
        for n, want in REJECTED.items():
            e = [r for r in self.res[n]["cases"] if r["suite"] == "yaml-test-suite"
                 and r["op"] == "events" and r["expect"] == "error"]
            self.assertEqual(len(e), 94)
            self.assertEqual(sum(r["loose"] == "pass" for r in e), want, n)

    def test_no_event_stream_means_valid_input_is_declined_not_failed(self):
        for n in VALUE_PASSES:
            ev = [r for r in self.res[n]["cases"] if r["suite"] == "yaml-test-suite"
                  and r["op"] == "events" and r["expect"] == "events"]
            self.assertEqual(len(ev), 308)
            self.assertEqual(sum(r["loose"] == "pass" for r in ev), 0, n)
            # Declined, and a few failed because the library rejected valid input.
            self.assertTrue(all(r["loose"] in ("unsupported", "fail") for r in ev), n)
            declined = [r for r in ev if r["loose"] == "unsupported"]
            self.assertTrue(all(r["reason"] == "adapter" for r in declined), n)

    def test_reference_cases_a_and_b(self):
        got = {n: (self.by[n][CASE_A]["loose"], self.by[n][CASE_B]["loose"]) for n in self.by}
        self.assertEqual(got["goccy-go-yaml"], ("pass", "fail"))
        self.assertEqual(got["palantir-yamlpatch-goccy"], ("pass", "fail"))
        # The Node libraries keep the block style and recompute the header, but lose the
        # blank line in front of the comment.
        for n in ("go-yaml-v3", "go-yaml-v4", "palantir-yamlpatch-v3"):
            self.assertEqual(got[n][0], "fail", n)
            self.assertIn("\\n#abc 123", self.by[n][CASE_A]["detail"], n)
        for n in ("go-yaml-v2", "sigs-k8s-yaml"):
            self.assertEqual(got[n], ("unsupported", "unsupported"), n)

    def test_adapter_made_behaviour_is_marked_and_the_library_edits_are_not_credited(self):
        for n in ("go-yaml-v3", "go-yaml-v4", "goccy-go-yaml"):
            self.assertEqual(self.res[n]["adapter"]["emulates"], ["error", "pointer", "unchanged"])
            ok = [r for r in self.res[n]["cases"] if r["op"] == "patch" and r["expect"] == "bytes"
                  and r["loose"] != "unsupported"]
            self.assertTrue(ok)
            self.assertTrue(all("pointer" in r.get("emulated", []) for r in ok), n)
        # The patcher does everything itself; only the reporting of the original bytes is the adapter's.
        for n in ("palantir-yamlpatch-v3", "palantir-yamlpatch-goccy"):
            self.assertEqual(self.res[n]["adapter"]["emulates"], ["unchanged"])
            marked = {e for r in self.res[n]["cases"] for e in r.get("emulated", [])}
            self.assertEqual(marked, {"unchanged"}, n)
            self.assertNotIn("emulated", self.by[n][CASE_A])

    def test_libraries_that_write_through_aliases_or_guess_keys_fail_those_cases(self):
        for n in ("go-yaml-v3", "go-yaml-v4", "palantir-yamlpatch-v3"):
            self.assertEqual(self.by[n]["own:edit/refuse-write-through-alias"]["loose"], "fail", n)
        self.assertEqual(self.by["palantir-yamlpatch-v3"]["own:edit/refuse-ambiguous-key"]["loose"], "fail")
        # A missing path is silently ignored by the library's replace.
        self.assertEqual(self.by["goccy-go-yaml"]["own:edit/atomic-failure"]["loose"], "fail")

    def test_safety_outcomes_are_the_librarys(self):
        for n in ("go-yaml-v2", "go-yaml-v3", "go-yaml-v4", "sigs-k8s-yaml"):
            for cid in ("own:safety/deeply-nested-flow-sequences", "own:safety/alias-expansion-bomb"):
                r = self.by[n][cid]
                self.assertEqual(r["loose"], "pass", (n, cid))
                self.assertEqual(r["got"]["status"], "error")
                self.assertLess(r["measured"]["seconds"], 10)
                # Memory is measured for the Go process the kit started, through its session.
                self.assertIsNotNone(r["measured"]["peak_mb"], (n, cid))
        # The bomb is expanded when the value is built: the memory bound is what stops it.
        bomb = self.by["goccy-go-yaml"]["own:safety/alias-expansion-bomb"]
        self.assertEqual(bomb["loose"], "fail")
        self.assertIn("memory", bomb["detail"])

    def test_a_deviation_list_is_checked_in_both_directions(self):
        for n in ("go-yaml-v3", "goccy-go-yaml", "palantir-yamlpatch-goccy"):
            res = self.res[n]
            entries = res["deviations"]["entries"]
            self.assertTrue(verify_deviations(res["cases"], entries, True)["ok"])
            # An entry removed: the failure is unlisted.
            fail = next(e for e in entries if e["verdict"] == "fail")
            rest = [e for e in entries if e is not fail]
            self.assertFalse(verify_deviations(res["cases"], rest, True)["ok"])
            # An entry that no longer describes the verdict: stale.
            flipped = [dict(e, verdict="unsupported") if e is fail else e for e in entries]
            self.assertTrue(verify_deviations(res["cases"], flipped, True)["stale"])
            # An entry for a case that is not there.
            ghost = entries + [dict(fail, case="own:edit/no-such-case")]
            self.assertTrue(verify_deviations(res["cases"], ghost, True)["unknown"])

    def test_the_report_names_every_library(self):
        md = report.render(list(self.res.values()))
        for n in GO:
            self.assertIn(n, md)


if __name__ == "__main__":
    unittest.main()
