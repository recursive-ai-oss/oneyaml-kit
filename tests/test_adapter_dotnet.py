"""End to end: the four .NET adapters through the whole pipeline.

Needs the .NET SDK (and network access for the first package restore), so it only runs when
KIT_ADAPTER_TESTS=1 (the gate sets it) and a `dotnet` executable is found (`DOTNET` names it when
it is not on PATH).
"""

import base64
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import support
from oneyaml_kit import cases as C
from oneyaml_kit import report
from oneyaml_kit.protocol import SubprocessAdapter
from oneyaml_kit.runner import (ADAPTERS, AdapterUnavailable, load_deviations, load_manifest, run_adapter,
                                unavailable_reason, validate_result, verify_deviations)

NAMES = ("yamldotnet", "sharpyaml", "vyaml", "yaml2jsonnode")
PINS = {"yamldotnet": "18.1.0", "sharpyaml": "3.16.0", "vyaml": "1.4.1", "yaml2jsonnode": "3.0.1"}
HAVE_DOTNET = shutil.which(os.environ.get("DOTNET", "dotnet")) is not None


def count(res, suite, op, expect, verdict, mode="loose"):
    return sum(1 for r in res["cases"]
               if (r["suite"], r["op"], r["expect"], r[mode]) == (suite, op, expect, verdict))


@unittest.skipUnless(os.environ.get("KIT_ADAPTER_TESTS") == "1" and HAVE_DOTNET,
                     "set KIT_ADAPTER_TESTS=1 and provide dotnet to build and run the .NET adapters")
class DotnetAdapters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cases = C.load_all()
        cls.res = support.run_adapters(NAMES, cases, workers=3, slowest=("yaml2jsonnode",))
        cls.by_id = {n: {r["id"]: r for r in cls.res[n]["cases"]} for n in NAMES}

    def test_results_are_well_formed_and_every_deviation_list_holds(self):
        for n, res in self.res.items():
            self.assertEqual(validate_result(res), [], n)
            d = res["deviations"]
            self.assertEqual((d["unlisted"], d["stale"], d["unknown"]), ([], [], []), n)
            self.assertEqual(len(res["cases"]), len(C.load_all()), n)

    def test_each_library_is_pinned_to_its_exact_release(self):
        for n, res in self.res.items():
            self.assertEqual(res["adapter"]["version"], PINS[n], n)
            self.assertEqual(res["adapter"]["language"], "csharp")
            self.assertEqual(res["adapter"]["protocol"], 2, n)
        info = self.res["yaml2jsonnode"]["adapter"]["info"]
        self.assertIn("JsonPatch.Net 5.0.3", info["json patch"])
        self.assertIn("YamlDotNet 18.1.0", info["yaml parser"])

    def test_capabilities_say_what_each_library_can_do(self):
        caps = {n: set(self.res[n]["adapter"]["capabilities"]) for n in NAMES}
        patch = {n: sorted(c for c in caps[n] if c.startswith("patch:")) for n in NAMES}
        self.assertEqual(patch["yamldotnet"], [])
        self.assertEqual(patch["vyaml"], [])
        self.assertEqual(patch["sharpyaml"], ["patch:replace"])
        self.assertEqual(patch["yaml2jsonnode"], sorted("patch:" + o for o in
                                                        ("add", "remove", "replace", "move", "copy", "test")))
        # No event stream: the library can only say whether it takes the input.
        self.assertNotIn("events", caps["yaml2jsonnode"])
        self.assertIn("outcome:events", caps["yaml2jsonnode"])
        for n in ("yamldotnet", "sharpyaml", "vyaml"):
            self.assertIn("events", caps[n])
            self.assertIn("error-events", caps[n])
            self.assertNotIn("outcome:events", caps[n])
        # VYaml's exceptions carry no position the adapter can read without parsing the message.
        self.assertNotIn("error-position", caps["vyaml"])
        for n in ("yamldotnet", "sharpyaml", "yaml2jsonnode"):
            self.assertIn("error-position", caps[n])

    def test_event_scores_agree_with_an_independent_formatter(self):
        # These counts were first obtained with a separate formatter written outside the kit: it printed
        # each parser's events in the suite's notation and compared them in full with the expected events,
        # on the same release of the suite. The kit agrees with it except where a library crashes: the kit
        # does not count a crash as a rejection, so a crash on an invalid input is a failure here.
        y = self.res["yamldotnet"]
        self.assertEqual(count(y, "yaml-test-suite", "events", "events", "pass"), 300)
        self.assertEqual(count(y, "yaml-test-suite", "events", "error", "pass"), 84)
        s = self.res["sharpyaml"]
        # 246 on SharpYaml 3.14.0; 3.16.0 reads four more valid inputs (52DL, 8MK2, S4JQ, UKK6/02).
        self.assertEqual(count(s, "yaml-test-suite", "events", "events", "pass"), 250)
        self.assertEqual(count(s, "yaml-test-suite", "events", "error", "pass"), 76)
        # VYaml: invalid inputs rejected, as the separate formatter counted them (its valid count ignored style).
        v = self.res["vyaml"]
        self.assertEqual(count(v, "yaml-test-suite", "events", "error", "pass"), 64)

    def test_a_library_crash_is_not_a_rejection(self):
        # YamlDotNet's scanner throws InvalidOperationException on these two invalid inputs. A rejection
        # would pass them; a crash fails them, and the kit starts a new adapter for the next case.
        for cid in ("yts:9C9N", "yts:VJP3/00"):
            r = self.by_id["yamldotnet"][cid]
            self.assertEqual((r["loose"], r["strict"], r["got"]["status"]), ("fail", "fail", "crash"), cid)
        # SharpYaml and VYaml leak the runtime's own exceptions in the same way.
        r = self.by_id["sharpyaml"]["own:schema/core-integers-beyond-64-bits"]
        self.assertEqual(r["loose"], "fail")
        self.assertIn("adapter exited", r["detail"])
        for cid in ("own:schema/duplicate-plain-keys-are-an-error", "own:schema/empty-key-is-null"):
            self.assertEqual(self.by_id["vyaml"][cid]["loose"], "fail", cid)
        self.assertGreaterEqual(self.res["yamldotnet"]["run"]["adapter_restarts"], 2)

    def test_a_rejection_in_the_librarys_own_hierarchy_is_still_a_rejection(self):
        for n in ("yamldotnet", "sharpyaml", "vyaml"):
            r = self.by_id[n]["own:diagnostics/undefined-alias"]
            self.assertEqual(r["loose"], "pass", n)
            self.assertNotEqual(r["got"]["status"], "crash", n)

    def test_vyaml_events_read_before_an_error_include_the_open_document(self):
        # The tokenizer's facts about a document that is still open when it throws used to be dropped, which
        # cut the event stream after the first line. The adapter, not the library, was at fault.
        r = self.by_id["vyaml"]["yts:2CMS"]
        self.assertEqual((r["loose"], r["strict"]), ("pass", "pass"))
        self.assertIn("style", r["emulated"])

    def test_byte_strings_are_declined_or_judged_by_what_the_library_writes(self):
        # YamlDotNet and SharpYaml write a byte array as a list of integers: not judged. VYaml has a byte
        # string and writes it as untagged base64 text, which reads back as a string: a failure.
        cid = "own:emit/binary-values-are-tagged-base64"
        for n in ("yamldotnet", "sharpyaml"):
            r = self.by_id[n][cid]
            self.assertEqual((r["loose"], r["strict"]), ("unsupported", "unsupported"), n)
            self.assertIn("list of integers", r["detail"], n)
        r = self.by_id["vyaml"][cid]
        self.assertEqual((r["loose"], r["strict"]), ("fail", "fail"))

    def test_columns_count_characters_not_utf16_code_units(self):
        # A character outside the Basic Multilingual Plane is two UTF-16 code units. YamlDotNet and SharpYaml
        # count code units; the adapters convert, so a library that reports the right character passes.
        cid = "own:diagnostics/position-counts-characters-outside-the-basic-plane"
        for n in ("yamldotnet", "sharpyaml", "yaml2jsonnode"):
            r = self.by_id[n][cid]
            self.assertEqual((r["loose"], r["strict"]), ("pass", "pass"), n)
            self.assertEqual((r["got"]["line"], r["got"]["column"]), (1, 4), n)

    def test_style_the_library_does_not_report_is_declared_not_hidden(self):
        v = self.res["vyaml"]
        self.assertEqual(v["adapter"]["emulates"], ["decode", "style", "tag"])
        r = self.by_id["vyaml"]["own:parse/anchor-and-alias"]
        self.assertEqual((r["loose"], r["strict"], r["emulated"]), ("pass", "pass", ["style"]))
        # A tag handle that a %TAG directive defines cannot be resolved, so the adapter declines.
        t = self.by_id["vyaml"]["own:parse/tag-handle-directive"]
        self.assertEqual((t["loose"], t["reason"]), ("unsupported", "adapter"))

    def test_reference_cases_through_each_edit_path(self):
        a, b = ("own:edit/block-literal-keeps-style-with-newline",
                "own:edit/block-literal-strip-when-no-trailing-newline")
        s = self.by_id["sharpyaml"]
        for cid in (a, b):
            self.assertEqual((s[cid]["loose"], s[cid]["strict"]), ("pass", "pass"), cid)
            # The span and the replacement text are the adapter's work, and the report says so.
            self.assertEqual(s[cid]["emulated"], ["pointer", "source-text"], cid)
        j = self.by_id["yaml2jsonnode"]
        for cid in (a, b):
            self.assertEqual(j[cid]["loose"], "fail", cid)
        self.assertIn("first difference at byte 0", j[b]["detail"])
        self.assertEqual(j[a].get("emulated"), None)

    def test_sharpyaml_supplies_no_refusal_of_its_own(self):
        s = self.by_id["sharpyaml"]
        # The adapter resolves pointers as a user of the library's events would: an alias is followed to the
        # anchored node and the first of two equal keys is taken. Neither write is refused, so the cases that
        # expect a refusal fail, and the library is not credited with one.
        for cid in ("own:edit/refuse-write-through-alias", "own:edit/refuse-ambiguous-key"):
            self.assertEqual((s[cid]["loose"], s[cid]["strict"]), ("fail", "fail"), cid)
            self.assertIn("accepted", s[cid]["detail"], cid)
            self.assertEqual(s[cid]["emulated"], ["pointer", "source-text"], cid)
        # A missing path or malformed pointer is an error the adapter raises, and says so.
        for cid in ("own:edit/replace-missing-path", "own:edit/atomic-failure"):
            self.assertEqual(s[cid]["strict"], "pass", cid)
            self.assertEqual(s[cid]["emulated"], ["error", "pointer", "unchanged"], cid)
        # Rejecting a pointer that is not one (no leading slash) happens before any resolution.
        self.assertEqual(s["own:edit/pointer-without-leading-slash"]["emulated"], ["error", "unchanged"])
        # Reading the byte order mark and the line endings back is the one place decoding mattered.
        self.assertIn("decode", s["own:edit/bom-and-crlf-kept"]["emulated"])

    def test_yaml2jsonnode_patch_failures_come_from_the_library(self):
        j = self.by_id["yaml2jsonnode"]
        # A failing `test` and a missing path are the library's results; only their category is the adapter's.
        t = j["own:edit/failed-test-changes-nothing"]
        self.assertEqual(t["loose"], "pass")
        self.assertEqual(t["emulated"], ["classification"])
        # Refusals do not exist in this library: it edits an expanded copy of the data.
        self.assertEqual(j["own:edit/refuse-write-through-alias"]["loose"], "fail")
        # The ambiguous-key document does not even load (two keys become one name), so the error is
        # reported, but not as a refusal.
        a = j["own:edit/refuse-ambiguous-key"]
        self.assertEqual((a["loose"], a["strict"]), ("pass", "fail"))
        self.assertIn("category", a["detail"])

    def test_memory_bombs_are_graded_on_the_cost_the_kit_measured(self):
        bomb = "own:safety/alias-expansion-bomb"
        # SharpYaml and Yaml2JsonNode expand the aliases and run past the memory bound: the kit stops them.
        for n in ("sharpyaml", "yaml2jsonnode"):
            r = self.by_id[n][bomb]
            # Whether memory or time runs out first depends on the machine; both are the kit's stop.
            self.assertEqual(r["loose"], "fail", n)
            self.assertIn(r["got"]["status"], ("memory", "timeout"), n)
            # A bounded case runs in a session of its own, so the kit stopping it is not a restart of
            # the main session; what counts is that such sessions were used.
            self.assertGreaterEqual(self.res[n]["run"]["bounded_sessions"], 1, n)
        # The other two keep the aliases shared, so the adapter answers `accepted`; the case allows only a
        # limit error, so taking the input is the failure, and the measured cost is recorded.
        for n in ("yamldotnet", "vyaml"):
            r = self.by_id[n][bomb]
            self.assertEqual((r["loose"], r["got"]["status"]), ("fail", "accepted"), n)
            self.assertIsNotNone(r["measured"]["peak_mb"], n)

    def test_deep_nesting_is_answered_inside_the_bounds(self):
        for n in ("yamldotnet", "vyaml"):
            r = self.by_id[n]["own:safety/deeply-nested-flow-sequences"]
            self.assertEqual((r["loose"], r["strict"]), ("pass", "pass"), n)
            self.assertLess(r["measured"]["seconds"], r["measured"]["time_bound"])
            self.assertIsNotNone(r["measured"]["peak_mb"])
        r = self.by_id["sharpyaml"]["own:safety/deeply-nested-flow-sequences"]
        self.assertEqual((r["loose"], r["strict"]), ("pass", "fail"))
        self.assertIn("limit", r["detail"])

    def test_a_stack_overflow_is_a_crash_of_that_case_only(self):
        m = load_manifest("yaml2jsonnode")
        ad = SubprocessAdapter(m["command"], cwd=m["_dir"], timeout=30)
        ad.start()
        try:
            deep = b"[" * 100000 + b"]" * 100000
            r = ad.run("deep", "value", deep, {})
            self.assertEqual(r["status"], "crash")
            ok = ad.run("after", "value", b"a: 1\n", {})
            self.assertEqual((ok["status"], ok["value"]), ("ok", [{"a": 1}]))
            self.assertEqual(ad.restarts, 1)
        finally:
            ad.close()

    def test_yamldotnet_names_a_refused_tag_and_only_a_refused_tag(self):
        m = load_manifest("yamldotnet")
        ad = SubprocessAdapter(m["command"], cwd=m["_dir"], timeout=30)
        ad.start()
        try:
            tagged = (b"!color red\n", b"- !shape {kind: circle}\n", b"!!python/object/apply:os.system [x]\n")
            for i, doc in enumerate(tagged):
                r = ad.run("tag%d" % i, "value", doc, {})
                self.assertEqual((r["status"], r["error"]["category"]), ("error", "unresolved-tag"), doc)
            # Other refusals from the same call must keep their own names, so the mapping cannot have widened.
            for i, doc in enumerate((b"a: [1\n", b"a: b: c\n", b"*nope\n", b"a: 'open\n")):
                r = ad.run("bad%d" % i, "value", doc, {})
                self.assertEqual(r["status"], "error", doc)
                self.assertNotEqual(r["error"]["category"], "unresolved-tag", doc)
            # The parser alone takes tags: only the deserializer refuses them.
            r = ad.run("ev", "events", b"!color red\n", {})
            self.assertEqual(r["status"], "ok")
        finally:
            ad.close()

    def test_repeated_keys_are_named_where_the_library_gives_a_signal_and_only_there(self):
        for n in ("sharpyaml", "yaml2jsonnode"):
            support.check_duplicate_key_category(self, n)

    def test_a_missing_document_reports_the_document_as_the_library_would_write_it(self):
        m = load_manifest("yaml2jsonnode")
        ad = SubprocessAdapter(m["command"], cwd=m["_dir"], timeout=30)
        ad.start()
        try:
            args = {"patch": [{"op": "replace", "path": "/a", "value": 2}], "document": 4}
            r = ad.run("missing", "patch", b"a:   1   # note\n", args)
            self.assertEqual((r["status"], r["error"]["category"]), ("error", "patch-path"))
            # Not the input bytes: this library can only write the data again.
            self.assertEqual(base64.b64decode(r["output"]), b"a: 1\n")
            self.assertEqual(r["emulated"], ["error"])
        finally:
            ad.close()

    def test_unlisting_a_failure_or_keeping_a_stale_entry_fails_the_check(self):
        for n in NAMES:
            entries = load_deviations(load_manifest(n))
            cases = self.res[n]["cases"]
            self.assertTrue(verify_deviations(cases, entries, True)["ok"], n)
            self.assertFalse(verify_deviations(cases, entries[1:], True)["ok"], n)
            stale = entries + [{"case": "own:edit/add-key", "expectation": "primary", "mode": "loose",
                                "verdict": "fail", "reason": "x"}]
            if not any(e["case"] == "own:edit/add-key" for e in entries):
                self.assertFalse(verify_deviations(cases, stale, True)["ok"], n)

    def test_every_deviation_has_a_reason_and_names_a_real_case(self):
        ids = {c.id for c in C.load_all()} | C.slow_ids()  # the fast tier leaves slow cases out
        for n in NAMES:
            for e in load_deviations(load_manifest(n)):
                self.assertIn(e["case"], ids, n)
                self.assertGreater(len(e["reason"]), 10, e)

    def test_the_report_shows_the_emulated_column_and_every_library(self):
        md = report.render(list(self.res.values()))
        for n in NAMES:
            self.assertIn(n, md)

    def test_manifests_use_only_the_documented_keys(self):
        for n in NAMES:
            m = load_manifest(n)
            self.assertEqual(m["language"], "csharp")
            self.assertNotIn("memory_limit_mb", m)  # the runtime reserves address space far beyond its use
            self.assertTrue((ADAPTERS / n / "packages.lock.json").is_file(), n)


@unittest.skipUnless(os.environ.get("KIT_ADAPTER_TESTS") == "1" and HAVE_DOTNET,
                     "set KIT_ADAPTER_TESTS=1 and provide dotnet to build and run the .NET adapters")
class StaleBuildOutput(unittest.TestCase):
    """Output left inside a project folder by an earlier run must neither break nor feed the next build."""

    SRC = ADAPTERS

    def build(self, props_text=None):
        work = Path(tempfile.mkdtemp(prefix="stale-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, work, True)
        skip = shutil.ignore_patterns("bin", "obj", "artifacts")
        (work / "adapters").mkdir()
        for name in ("yamldotnet", "dotnet-common"):
            shutil.copytree(self.SRC / name, work / "adapters" / name, ignore=skip)
        shutil.copy(self.SRC / "Directory.Build.props", work / "adapters")
        if props_text is not None:
            (work / "adapters" / "Directory.Build.props").write_text(props_text)
        # Leftovers of the three shapes an earlier run can produce, with source files that cannot compile.
        for name in ("yamldotnet", "dotnet-common"):
            for left in ("artifacts/obj/Release", "artifacts/bin", "bin/Release", "obj"):
                d = work / "adapters" / name / left
                d.mkdir(parents=True)
                (d / "leftover.cs").write_text("this is not C#\n")
        art = work / "out"
        env = dict(os.environ, KIT_DOTNET_ARTIFACTS=str(art))
        p = subprocess.run(["sh", str(work / "adapters" / "yamldotnet" / "build.sh")], env=env,
                           capture_output=True, text=True, timeout=250)
        return p, art

    def test_leftover_folders_in_the_project_are_not_built_and_not_used(self):
        p, art = self.build()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((art / "bin" / "YamlDotNetAdapter" / "release" / "OneYamlKit.Adapter.YamlDotNet.dll").is_file())

    def test_the_leftovers_do_break_a_build_without_the_exclusion(self):
        # Proves the case above bites: without the exclusion the same state fails to compile.
        props = (self.SRC / "Directory.Build.props").read_text()
        self.assertIn("artifacts/**", props)
        bare = ("<Project>\n  <PropertyGroup Condition=\"'$(KIT_DOTNET_ARTIFACTS)' != ''\">\n"
                "    <ArtifactsPath>$(KIT_DOTNET_ARTIFACTS)</ArtifactsPath>\n  </PropertyGroup>\n</Project>\n")
        p, _ = self.build(bare)
        self.assertNotEqual(p.returncode, 0)


OPTIONAL = "oneyaml-csharp"
VAR = "KIT_ONEYAML_CSHARP_SRC"


class OptionalAdapter(unittest.TestCase):
    """The oneyaml-csharp adapter links a library the kit cannot fetch, so it is built only when
    KIT_ONEYAML_CSHARP_SRC names a checkout, and is skipped, and said to be, otherwise."""

    def test_it_is_registered_as_needing_the_checkout(self):
        m = load_manifest(OPTIONAL)
        self.assertEqual(m["language"], "csharp")
        self.assertEqual(m["requires_env"], [VAR])
        load_deviations(m)  # the list loads; its entries are checked against a run below
        d = ADAPTERS / OPTIONAL
        proj = (d / "OneYamlAdapter.csproj").read_text(encoding="utf-8")
        self.assertNotIn("PackageReference", proj)  # the library is a checkout, not a package
        self.assertIn("ONEYAML_LINKED", proj)
        self.assertFalse((d / "packages.lock.json").exists())
        for script in ("build.sh", "run.sh"):
            self.assertIn("KIT_DOTNET_ARTIFACTS", (d / script).read_text(encoding="utf-8"))

    def test_without_the_checkout_it_is_unavailable_and_the_run_says_so(self):
        env = {k: v for k, v in os.environ.items() if k != VAR}
        with self.assertRaises(AdapterUnavailable) as cm:
            run_adapter(OPTIONAL, [], complete=False, env=env)
        self.assertIn(VAR, str(cm.exception))
        m = load_manifest(OPTIONAL)
        self.assertIsNotNone(unavailable_reason(m, dict(env, **{VAR: ""})))
        self.assertIsNone(unavailable_reason(m, dict(env, **{VAR: "/x"})))

    def test_the_cli_skips_it_without_failing(self):
        env = {k: v for k, v in os.environ.items() if k != VAR}
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or None) as out:
            r = subprocess.run(["python3", "bin/kit", "fast", "--adapter", OPTIONAL, "--out", out],
                               cwd=support.ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SKIPPED", r.stdout)


@unittest.skipUnless(os.environ.get("KIT_ADAPTER_TESTS") == "1" and HAVE_DOTNET and os.environ.get(VAR),
                     f"needs KIT_ADAPTER_TESTS=1, dotnet and {VAR}")
class OptionalAdapterRun(unittest.TestCase):
    def test_it_builds_and_every_failure_is_listed(self):
        res = run_adapter(OPTIONAL, C.load_all(), complete=True)
        self.assertEqual(validate_result(res), [])
        caps = set(res["adapter"]["capabilities"])
        # What is wired: events, values, errors with category and position, dump, and RFC 6902 and RFC 7396 patches.
        self.assertTrue({"events", "value", "error-category", "error-position"} <= caps)
        self.assertEqual(res["adapter"]["info"]["linked"], "yes")
        d = res["deviations"]
        self.assertEqual((d["ok"], d["unlisted"], d["stale"], d["unknown"]), (True, [], [], []))
        self.assertTrue(any(r["loose"] == "pass" for r in res["cases"]))


if __name__ == "__main__":
    unittest.main()
