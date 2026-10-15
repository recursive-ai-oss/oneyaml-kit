"""The Go adapters: pins, honest declarations and the numbers they produce.

The static checks always run. Building and running the adapters needs a Go toolchain and
a populated module cache, so that part only runs when KIT_ADAPTER_TESTS=1 (the gate sets it).
"""

import os
import re
import subprocess
import tempfile
import unittest

import support
from oneyaml_kit import cases as C
from oneyaml_kit import report, runner
from oneyaml_kit.runner import run_adapter, validate_result, verify_deviations

ADAPTERS = support.ROOT / "adapters"
# oneyaml-go needs a checkout of the library, so it is optional: it has its own tests below
# and is not part of the run over the adapters that always build.
OPTIONAL = "oneyaml-go"
GO = sorted(p.name for p in ADAPTERS.iterdir()
            if (p / "go.mod").is_file() and (p / "adapter.json").is_file() and p.name != OPTIONAL)

# What each library can do, from the library's own API. Declaring more would be a claim
# the kit then judges; declaring less hides results.
CAPS = {
    "go-yaml-v2": {"outcome:events", "value", "dump"},
    "go-yaml-v3": {"outcome:events", "value", "dump", "patch:add", "patch:remove", "patch:replace"},
    "go-yaml-v4": {"outcome:events", "value", "dump", "error-category", "error-position",
                   "patch:add", "patch:remove", "patch:replace"},
    "goccy-go-yaml": {"outcome:events", "value", "dump", "error-category", "error-position", "patch:replace"},
    "sigs-k8s-yaml": {"outcome:events", "value", "dump"},
    "palantir-yamlpatch-v3": {"patch:add", "patch:remove", "patch:replace", "patch:move",
                              "patch:copy", "patch:test"},
    "palantir-yamlpatch-goccy": {"patch:add", "patch:remove", "patch:replace", "patch:move",
                                 "patch:copy", "patch:test"},
}

# yaml-test-suite results for the parsers: how many valid inputs load to a value equal to
# the suite's JSON, and how many invalid inputs are rejected (the number to compare with
# any plain "does it load this file" probe of the same library). Pinned so that a change in
# an adapter or a library shows up here.
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


class Optional(unittest.TestCase):
    """The oneyaml-go adapter links a library that the kit cannot fetch, so it is built only when
    KIT_ONEYAML_GO_SRC names a checkout, and is skipped, and said to be, otherwise."""

    def test_it_is_registered_as_needing_the_checkout(self):
        m = runner.load_manifest(OPTIONAL)
        self.assertEqual(m["language"], "go")
        self.assertEqual(m["requires_env"], ["KIT_ONEYAML_GO_SRC"])
        runner.load_deviations(m)  # the list is the library's own and changes with it
        mod = (ADAPTERS / OPTIONAL / "go.mod").read_text()
        self.assertNotIn("oneyaml.dev", mod)  # the build generates that requirement

    def test_without_the_checkout_it_is_unavailable_and_the_run_says_so(self):
        env = {k: v for k, v in os.environ.items() if k != "KIT_ONEYAML_GO_SRC"}
        with self.assertRaises(runner.AdapterUnavailable) as cm:
            run_adapter(OPTIONAL, [], complete=False, env=env)
        self.assertIn("KIT_ONEYAML_GO_SRC", str(cm.exception))
        # An empty value counts as unset.
        self.assertIsNotNone(runner.unavailable_reason(runner.load_manifest(OPTIONAL),
                                                       dict(env, KIT_ONEYAML_GO_SRC="")))
        self.assertIsNone(runner.unavailable_reason(runner.load_manifest(OPTIONAL),
                                                    dict(env, KIT_ONEYAML_GO_SRC="/x")))

    def test_the_report_names_a_skipped_adapter(self):
        md = report.render([], [(OPTIONAL, "environment variable KIT_ONEYAML_GO_SRC is not set")])
        self.assertIn("Not run", md)
        self.assertIn(OPTIONAL, md)

    def test_the_cli_skips_it_without_failing(self):
        env = {k: v for k, v in os.environ.items() if k != "KIT_ONEYAML_GO_SRC"}
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or None) as out:
            r = subprocess.run(["python3", "bin/kit", "fast", "--adapter", OPTIONAL, "--out", out],
                               cwd=support.ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SKIPPED", r.stdout)


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

    def test_without_an_event_stream_acceptance_is_judged_and_events_are_not_claimed(self):
        for n in VALUE_PASSES:
            self.assertIn("outcome:events", self.res[n]["adapter"]["capabilities"])
            self.assertNotIn("events", self.res[n]["adapter"]["capabilities"])
            self.assertEqual(self.res[n]["adapter"]["protocol"], 2)
            ev = [r for r in self.res[n]["cases"] if r["suite"] == "yaml-test-suite"
                  and r["op"] == "events" and r["expect"] == "events"]
            self.assertEqual(len(ev), 308)
            # No event is compared, so none passes; a valid input the library rejects fails.
            self.assertEqual(sum(r["loose"] == "pass" for r in ev), 0, n)
            self.assertTrue(all(r["loose"] in ("unsupported", "fail") for r in ev), n)
        # An invalid input the library accepts is a failure, not a declined case.
        bad = self.by["go-yaml-v3"]["own:safety/invalid-utf-8-byte"]
        self.assertEqual(bad["loose"], "pass")
        accepted = [r for r in self.res["goccy-go-yaml"]["cases"] if r["op"] == "events"
                    and r["expect"] == "error" and r["loose"] == "fail"]
        self.assertTrue(accepted)
        self.assertTrue(all(r["got"]["status"] == "accepted" for r in accepted))

    def test_backend_versions_in_hello_are_the_ones_the_module_pins(self):
        gomod = {n: (ADAPTERS / n / "go.mod").read_text() for n in GO}
        v3 = self.res["palantir-yamlpatch-v3"]["adapter"]["info"]["backend"]
        self.assertIn("gopkg.in/yaml.v3 v3.0.1", v3)
        self.assertRegex(gomod["palantir-yamlpatch-v3"], r"(?m)^\s*gopkg.in/yaml.v3 v3.0.1")
        g = self.res["palantir-yamlpatch-goccy"]["adapter"]["info"]["backend"]
        self.assertIn("github.com/goccy/go-yaml v1.15.22", g)
        self.assertRegex(gomod["palantir-yamlpatch-goccy"], r"(?m)^\s*github.com/goccy/go-yaml v1.15.22")

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
                  and r["id"].startswith("own:edit/") and r["loose"] != "unsupported"
                  # An error before any path was resolved (the library rejected the document)
                  # has no pointer step to mark; every result that wrote bytes does.
                  and not (r.get("detail") or "").startswith("unexpected error")]
            self.assertTrue(ok)
            self.assertTrue(all("pointer" in r.get("emulated", []) for r in ok), n)
        # The patcher does everything itself; only the reporting of the original bytes is the adapter's.
        for n in ("palantir-yamlpatch-v3", "palantir-yamlpatch-goccy"):
            self.assertEqual(self.res[n]["adapter"]["emulates"], ["unchanged"])
            marked = {e for r in self.res[n]["cases"] for e in r.get("emulated", [])}
            self.assertEqual(marked, {"unchanged"}, n)
            self.assertNotIn("emulated", self.by[n][CASE_A])

    def test_the_adapters_resolver_follows_aliases_and_takes_the_first_key(self):
        # The adapters whose library has no pointer resolution give no protection the library
        # does not give: they follow an alias to the anchored node and take the first key that
        # matches, so the refusal cases show the library's own lack of one.
        for n in ("go-yaml-v3", "go-yaml-v4"):
            for cid in ("own:edit/refuse-write-through-alias", "own:edit/refuse-ambiguous-key"):
                r = self.by[n][cid]
                self.assertEqual(r["loose"], "fail", (n, cid))
                self.assertNotIn("error", r.get("emulated", []), (n, cid))
                self.assertEqual(r.get("emulated"), ["pointer"], (n, cid))
        # The patcher's own result, not the adapter's.
        for cid in ("own:edit/refuse-write-through-alias", "own:edit/refuse-ambiguous-key"):
            self.assertEqual(self.by["palantir-yamlpatch-v3"][cid]["loose"], "fail", cid)
        # A missing path is silently ignored by the library's replace.
        self.assertEqual(self.by["goccy-go-yaml"]["own:edit/atomic-failure"]["loose"], "fail")

    def test_safety_outcomes_are_the_librarys(self):
        for n in ("go-yaml-v2", "go-yaml-v3", "go-yaml-v4", "sigs-k8s-yaml"):
            for cid in ("own:safety/deeply-nested-flow-sequences", "own:safety/alias-expansion-bomb"):
                r = self.by[n][cid]
                self.assertEqual(r["loose"], "pass", (n, cid))
                self.assertEqual(r["got"]["status"], "error")
                self.assertLess(r["measured"]["seconds"], r["measured"]["time_bound"])
                # Memory is measured for the Go process the kit started, through its session.
                self.assertIsNotNone(r["measured"]["peak_mb"], (n, cid))
        # The bomb is expanded when the value is built, so a bound stops it. Which bound comes
        # first depends on how fast the machine allocates, so either one is the expected stop.
        bomb = self.by["goccy-go-yaml"]["own:safety/alias-expansion-bomb"]
        self.assertEqual((bomb["loose"], bomb["strict"]), ("fail", "fail"))
        self.assertIn(bomb["got"]["status"], ("memory", "timeout"))
        self.assertRegex(bomb["detail"], r"^(memory growth passed|no answer within)")

    def test_repeated_keys_are_named_where_the_library_gives_a_signal_and_only_there(self):
        for n in ("go-yaml-v4", "goccy-go-yaml"):
            support.check_duplicate_key_category(self, n)

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


@unittest.skipUnless(os.environ.get("KIT_ADAPTER_TESTS") == "1" and os.environ.get("KIT_ONEYAML_GO_SRC"),
                     "needs KIT_ADAPTER_TESTS=1 and KIT_ONEYAML_GO_SRC")
class OptionalRun(unittest.TestCase):
    def test_it_builds_declares_what_it_does_and_fails_nothing_unlisted(self):
        res = run_adapter(OPTIONAL, C.load_all(), complete=True)
        self.assertEqual(validate_result(res), [])
        self.assertEqual(set(res["adapter"]["capabilities"]),
                         {"events", "value", "error-category", "error-position", "patch:add", "patch:remove",
                          "patch:replace", "patch:move", "patch:copy", "patch:test", "patch:merge"})
        # The library is atomic and keeps the bytes of a refused patch, so nothing is emulated.
        self.assertEqual(res["adapter"]["emulates"], [])
        d = res["deviations"]
        self.assertEqual((d["ok"], d["unlisted"], d["stale"], d["unknown"]), (True, [], [], []))


if __name__ == "__main__":
    unittest.main()
