"""Protocol 2: accepted answers, error-events, alternatives, the vocabulary, deep answers."""

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import support
from oneyaml_kit import compare as K
from oneyaml_kit import runner as R
from oneyaml_kit import vocabulary
from oneyaml_kit.cases import Case
from oneyaml_kit.protocol import SubprocessAdapter

FAKE = str(Path(__file__).parent / "fixtures" / "fake_adapter.py")
PAIR = ["+STR", "-STR"]


def mk(cid="c", op="events", expect=None, args=None):
    expect = expect or {"kind": "events", "events": PAIR}
    return Case(id=cid, suite="own", category="parse", title="t", origin="bug", op=op,
                input=b"x\n", expect=expect, args=args or {})


def err(**kw):
    e = {"kind": "error", "category": "any", "line": None, "column": None, "events": None, "unchanged": False}
    e.update(kw)
    return e


def bounded(**kw):
    e = {"kind": "bounded", "time_seconds": 5, "memory_mb": 100, "accept": None, "category": ["limit"]}
    e.update(kw)
    return e


def one(case, resp, caps, protocol=2, emulates=()):
    return R.evaluate_case(case, resp, set(caps), 0, set(emulates), protocol)


MEASURE = {"seconds": 0.2, "peak_mb": 5.0}
OUTCOME = {"outcome:events", "outcome:value", "error-category", "error-position"}


class Alternatives(unittest.TestCase):
    def test_either_capability_will_do(self):
        c = mk()
        needs, _ = K.required_caps(c, c.expect)
        self.assertEqual(needs, {"events|outcome:events"})
        self.assertTrue(K.satisfied(needs, {"events"}))
        self.assertTrue(K.satisfied(needs, {"outcome:events"}))
        self.assertFalse(K.satisfied(needs, {"value", "outcome:value"}))
        v = mk(op="value", expect={"kind": "value", "value": []})
        self.assertEqual(K.required_caps(v, v.expect)[0], {"value|outcome:value"})

    def test_a_prefix_of_events_before_an_error_needs_error_events_in_strict_only(self):
        c = mk(expect=err(events=["+STR"]))
        self.assertEqual(K.required_caps(c, c.expect)[1], {"error-events"})
        self.assertEqual(K.required_caps(mk(expect=err()), err())[1], set())


class Accepted(unittest.TestCase):
    def test_accepting_an_input_that_must_be_rejected_is_a_failure_without_events(self):
        c = mk(expect=err())
        r = one(c, {"status": "accepted"}, OUTCOME)
        self.assertEqual((r["loose"], r["strict"]), ("fail", "fail"))
        self.assertIn("accepted", r["detail"])
        self.assertEqual(r["got"]["status"], "accepted")

    def test_a_valid_events_case_stays_unsupported_and_needs_no_deviation_entry(self):
        c = mk()
        r = one(c, {"status": "accepted"}, OUTCOME)
        self.assertEqual((r["loose"], r["strict"], r["reason"]), ("unsupported", "unsupported", "capability"))
        # An adapter that does claim events and still declines the answer must list it.
        r = one(c, {"status": "accepted"}, {"events", "error-events"})
        self.assertEqual(r["reason"], "adapter")

    def test_rejecting_valid_input_is_still_a_failure(self):
        r = one(mk(), {"status": "error", "error": {"category": "syntax", "message": "m"}}, OUTCOME)
        self.assertEqual((r["loose"], r["strict"]), ("fail", "fail"))

    def test_a_bound_case_is_graded_from_accepted_and_its_cost(self):
        c = mk(op="value", expect=bounded())
        ok = {"status": "accepted", "_measure": MEASURE}
        r = one(c, ok, OUTCOME)
        self.assertEqual((r["loose"], r["strict"]), ("fail", "fail"))
        self.assertIn("only a limit error is allowed", r["detail"])
        self.assertEqual(r["measured"], {"seconds": 0.2, "peak_mb": 5.0})
        slow = {"status": "accepted", "_measure": {"seconds": 9.0, "peak_mb": 5.0}}
        self.assertIn("took 9.00 s", one(c, slow, OUTCOME)["detail"])
        big = {"status": "accepted", "_measure": {"seconds": 0.1, "peak_mb": 500.0}}
        self.assertIn("peak memory growth", one(c, big, OUTCOME)["detail"])

    def test_accepted_within_bounds_when_a_result_was_also_allowed(self):
        c = mk(expect=bounded(accept={"kind": "events", "events": PAIR}))
        r = one(c, {"status": "accepted", "_measure": MEASURE}, OUTCOME)
        self.assertEqual((r["loose"], r["strict"], r["reason"]), ("unsupported", "unsupported", "capability"))
        r = one(c, {"status": "accepted", "_measure": MEASURE}, {"events", "error-events"})
        self.assertEqual(r["reason"], "adapter")
        # A bound broken is a failure whatever was or was not described.
        r = one(c, {"status": "accepted", "_measure": {"seconds": 99, "peak_mb": 1}}, OUTCOME)
        self.assertEqual((r["loose"], r["strict"]), ("fail", "fail"))

    def test_accepted_is_not_an_answer_to_a_patch(self):
        c = mk(op="patch", expect={"kind": "bytes", "bytes": b""}, args={"patch": [{"op": "add", "path": "/a"}]})
        r = one(c, {"status": "accepted"}, {"patch:add"})
        self.assertEqual((r["loose"], r["strict"]), ("fail", "fail"))

    def test_a_protocol_1_adapter_may_not_say_accepted(self):
        r = one(mk(expect=err()), {"status": "accepted"}, {"events"}, protocol=1)
        self.assertEqual((r["loose"], r["strict"]), ("fail", "fail"))
        self.assertIn("protocol 2", r["detail"])


class ErrorWithoutEvents(unittest.TestCase):
    def test_the_error_and_position_count_and_the_prefix_is_not_claimed(self):
        c = mk(expect=err(category="syntax", line=2, column=3, events=["+STR", "+DOC"]))
        caps = {"events", "error-category", "error-position"}  # no error-events
        good = {"status": "error", "error": {"category": "syntax", "line": 2, "column": 3}}
        r = one(c, good, caps)
        self.assertEqual((r["loose"], r["strict"]), ("pass", "unsupported"))
        self.assertIn("error-events", r["detail"])
        wrong = {"status": "error", "error": {"category": "syntax", "line": 3, "column": 3}}
        r = one(c, wrong, caps)
        self.assertEqual((r["loose"], r["strict"]), ("pass", "fail"))
        with_events = {**good, "events": ["+STR"]}
        self.assertEqual(one(c, with_events, caps | {"error-events"})["strict"], "fail")
        self.assertEqual(one(c, {**good, "events": ["+STR", "+DOC"]}, caps | {"error-events"})["strict"], "pass")

    def test_protocol_1_events_adapters_keep_their_prefix_checks(self):
        caps, proto = R.check_hello({"name": "a", "protocol": 1, "capabilities": ["events"]})
        self.assertEqual(proto, 1)
        self.assertIn("error-events", caps)


class Hello(unittest.TestCase):
    def test_versions_and_new_capabilities(self):
        self.assertEqual(R.check_hello({"name": "a", "protocol": 2, "capabilities": ["outcome:events"]})[1], 2)
        for bad in ({"protocol": 3, "capabilities": []}, {"protocol": 1, "capabilities": ["outcome:events"]},
                    {"protocol": 1, "capabilities": ["error-events"]}, {"protocol": 2, "capabilities": "events"},
                    {"protocol": 2, "capabilities": [1]}):
            with self.assertRaises(R.RunError, msg=str(bad)):
                R.check_hello({"name": "a", **bad})

    def test_emulated_behaviours_come_from_one_vocabulary(self):
        for name in vocabulary.EMULATES:
            R.check_hello({"name": "a", "protocol": 2, "capabilities": [], "emulates": [name]})
        with self.assertRaises(R.RunError) as cm:
            R.check_hello({"name": "a", "protocol": 2, "capabilities": [], "emulates": ["unchanged", "magic"]})
        self.assertIn("magic", str(cm.exception))
        with self.assertRaises(R.RunError):
            R.check_hello({"name": "a", "protocol": 1, "capabilities": [], "emulates": "unchanged"})

    def test_the_vocabulary_in_the_documentation_is_the_code_s(self):
        doc = (support.ROOT / "docs" / "adapter-protocol.md").read_text()
        for name, meaning in vocabulary.EMULATES.items():
            self.assertIn(f"| `{name}` |", doc, name)
            self.assertTrue(meaning)

    def test_every_emulated_name_the_adapters_use_is_in_the_vocabulary(self):
        used = set()
        for p in (support.ROOT / "adapters").rglob("*"):
            if p.suffix in (".go", ".cs", ".rs") and ".bin" not in p.parts and "target" not in p.parts:
                for line in p.read_text(errors="replace").splitlines():
                    m = re.search(r"Emulates\s*(?::|=>)|EMULATES\b[^=]*=", line)
                    if m and "//" not in line[:m.start()]:
                        used.update(re.findall(r'"([a-z-]+)"', line[m.end():]))
        self.assertGreaterEqual(len(used), 4, used)
        self.assertEqual(sorted(used - set(vocabulary.EMULATES)), [])


class SeveralCategories(unittest.TestCase):
    def test_a_bounded_case_can_allow_a_list_of_error_categories(self):
        c = mk(expect=bounded(category=["syntax", "limit"]))
        self.assertEqual(K.required_caps(c, c.expect)[1], {"error-category"})
        caps = {"events", "error-category"}
        for cat in ("syntax", "limit"):
            r = one(c, {"status": "error", "error": {"category": cat}, "_measure": MEASURE}, caps)
            self.assertEqual((r["loose"], r["strict"]), ("pass", "pass"), cat)
        # Any error passes loose; strict wants one of the named kinds, not "other".
        r = one(c, {"status": "error", "error": {"category": "other"}, "_measure": MEASURE}, caps)
        self.assertEqual((r["loose"], r["strict"]), ("pass", "fail"))
        self.assertIn("syntax, limit", r["detail"])
        r = one(c, {"status": "error", "error": {"category": "other"}, "_measure": MEASURE}, {"events"})
        self.assertEqual((r["loose"], r["strict"]), ("pass", "unsupported"))
        r = one(c, {"status": "ok", "events": PAIR, "_measure": MEASURE}, caps)
        self.assertEqual((r["loose"], r["strict"]), ("fail", "fail"))
        self.assertIn("a syntax or limit error", r["detail"])

    def test_accepted_to_a_bounded_patch_is_a_failure_not_a_silent_unsupported(self):
        patch = {"patch": [{"op": "add", "path": "/a", "value": 1}]}
        c = mk(op="patch", expect=bounded(accept={"kind": "bytes", "bytes": b""}), args=patch)
        r = one(c, {"status": "accepted", "_measure": MEASURE}, {"patch:add", "error-category"})
        self.assertEqual((r["loose"], r["strict"]), ("fail", "fail"))
        self.assertIn("not an answer to a patch", r["detail"])


class Subprocess(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, self.t)

    def adapter(self, script, caps, protocol="2"):
        (self.t / "script.json").write_text(json.dumps(script))
        env = dict(os.environ, FAKE_SCRIPT=str(self.t / "script.json"), FAKE_CAPS=json.dumps(caps),
                   FAKE_PROTOCOL=protocol)
        a = SubprocessAdapter([sys.executable, FAKE], env=env, timeout=20)
        self.addCleanup(a.close)
        return a

    def test_an_events_less_adapter_exposes_the_inputs_it_wrongly_accepts(self):
        a = self.adapter({"bad": "accepted", "good": "accepted"}, ["outcome:events"])
        h = a.start()
        caps, proto = R.check_hello(h)
        cases = [mk("bad", expect=err()), mk("good")]
        res = R.execute(a, caps, cases, protocol=proto)
        self.assertEqual([(r["loose"], r["strict"]) for r in res], [("fail", "fail"), ("unsupported", "unsupported")])
        self.assertEqual(res[1]["reason"], "capability")

    def test_a_response_nested_a_hundred_thousand_levels_deep_is_graded(self):
        a = self.adapter({"deep": "deep-value", "next": "ok-events"}, ["value", "events"])
        h = a.start()
        caps, proto = R.check_hello(h)
        n = 100_000
        deep = None
        for _ in range(n):
            deep = [deep]
        longer = [deep]
        want = Case(id="deep", suite="own", category="safety", title="t", origin="bug", op="value", input=b"x\n",
                    expect={"kind": "value", "value": deep, "tags": "keep"})
        wrong = Case(id="deep", suite="own", category="safety", title="t", origin="bug", op="value", input=b"x\n",
                     expect={"kind": "value", "value": longer, "tags": "keep"})
        res = R.execute(a, caps, [want, mk("next")], protocol=proto)
        self.assertEqual((res[0]["loose"], res[0]["strict"]), ("pass", "pass"), res[0])
        self.assertEqual(res[1]["loose"], "pass")
        self.assertEqual(a.restarts, 0)
        a2 = self.adapter({"deep": "deep-value", "next": "ok-events"}, ["value", "events"])
        h = a2.start()
        r = R.execute(a2, caps, [wrong, mk("next")], protocol=proto)
        self.assertEqual(r[0]["loose"], "fail")
        self.assertIn("expected", r[0]["detail"])
        self.assertEqual(r[1]["loose"], "pass")

    def test_an_unterminated_deep_response_fails_that_case_and_the_run_goes_on(self):
        a = self.adapter({"deep": "deep-garbage", "next": "ok-events"}, ["value", "events"])
        h = a.start()
        caps, proto = R.check_hello(h)
        case = Case(id="deep", suite="own", category="safety", title="t", origin="bug", op="value", input=b"x\n",
                    expect={"kind": "value", "value": [], "tags": "keep"})
        res = R.execute(a, caps, [case, mk("next")], protocol=proto)
        self.assertEqual(res[0]["loose"], "fail")
        self.assertEqual(res[1]["loose"], "pass")


class BuildOutput(unittest.TestCase):
    def test_build_output_defaults_to_the_output_directory(self):
        t = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, t)
        d = t / "adapters" / "fake"
        d.mkdir(parents=True)
        (d / "adapter.json").write_text(json.dumps({
            "format": R.ADAPTER_FORMAT, "name": "fake", "language": "python",
            "build": [sys.executable, "-c",
                      "import os; open('built.txt','w').write(repr([os.environ.get(k) for k in "
                      "('CARGO_TARGET_DIR','KIT_GO_BIN','KIT_DOTNET_ARTIFACTS')]))"],
            "command": [sys.executable, FAKE]}))
        (t / "script.json").write_text("{}")
        out = t / "out"
        mine = ("CARGO_TARGET_DIR", "KIT_GO_BIN", "KIT_DOTNET_ARTIFACTS")
        base = {k: v for k, v in os.environ.items() if k not in mine}
        env = dict(base, FAKE_SCRIPT=str(t / "script.json"))
        R.run_adapter("fake", [mk()], False, adapters_dir=t / "adapters", env=env, out=out)
        self.assertEqual(eval((d / "built.txt").read_text()),
                         [str(out / "target"), str(out / "gobin"), str(out / "artifacts")])
        # A location the user chose is kept.
        R.run_adapter("fake", [mk()], False, adapters_dir=t / "adapters", out=out,
                      env=dict(env, CARGO_TARGET_DIR="/somewhere/else"))
        self.assertEqual(eval((d / "built.txt").read_text())[0], "/somewhere/else")


if __name__ == "__main__":
    unittest.main()
