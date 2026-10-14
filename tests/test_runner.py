import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from oneyaml_kit import runner as R
from oneyaml_kit.cases import Case
from oneyaml_kit import protocol as P
from oneyaml_kit.protocol import AdapterError, SubprocessAdapter

FAKE = str(Path(__file__).parent / "fixtures" / "fake_adapter.py")


def mk(cid, kind="events", expect=None):
    expect = expect or {"kind": "events", "events": ["+STR", "-STR"]}
    return Case(id=cid, suite="own", category="parse", title="t", origin="bug", op="events",
                input=b"x\n", expect=expect)


class Subprocess(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, self.t)
        self.log = self.t / "log"

    def adapter(self, script, caps=None, timeout=1.0):
        (self.t / "script.json").write_text(json.dumps(script))
        env = dict(os.environ, FAKE_SCRIPT=str(self.t / "script.json"), FAKE_LOG=str(self.log))
        if caps is not None:
            env["FAKE_CAPS"] = json.dumps(caps)
        a = SubprocessAdapter([sys.executable, FAKE], env=env, timeout=timeout)
        self.addCleanup(a.close)
        return a

    def test_handshake_and_a_passing_case(self):
        a = self.adapter({})
        h = a.start()
        self.assertEqual((h["name"], h["protocol"]), ("fake", 1))
        res = R.execute(a, set(h["capabilities"]), [mk("a")])
        self.assertEqual((res[0]["loose"], res[0]["strict"]), ("pass", "pass"))

    def test_crash_fails_that_case_and_the_next_one_still_runs(self):
        a = self.adapter({"b": "crash"})
        h = a.start()
        res = R.execute(a, set(h["capabilities"]), [mk("a"), mk("b"), mk("c")])
        self.assertEqual([r["loose"] for r in res], ["pass", "fail", "pass"])
        self.assertIn("exited", res[1]["detail"])
        self.assertEqual(a.restarts, 1)

    def test_hang_times_out_and_the_run_continues(self):
        a = self.adapter({"b": "hang"}, timeout=0.5)
        h = a.start()
        res = R.execute(a, set(h["capabilities"]), [mk("a"), mk("b"), mk("c")])
        self.assertEqual([r["loose"] for r in res], ["pass", "fail", "pass"])
        self.assertIn("no answer", res[1]["detail"])

    def test_garbage_is_a_failure_not_a_pass(self):
        a = self.adapter({"b": "garbage"})
        h = a.start()
        res = R.execute(a, set(h["capabilities"]), [mk("b"), mk("c")])
        self.assertEqual([r["loose"] for r in res], ["fail", "pass"])

    def test_adapter_declining_a_case_is_unsupported_with_reason_adapter(self):
        a = self.adapter({"b": "unsupported"})
        h = a.start()
        res = R.execute(a, set(h["capabilities"]), [mk("b")])
        self.assertEqual((res[0]["loose"], res[0]["reason"]), ("unsupported", "adapter"))

    def test_undeclared_capability_is_not_sent(self):
        a = self.adapter({}, caps=["value"])
        h = a.start()
        res = R.execute(a, set(h["capabilities"]), [mk("a")])
        self.assertEqual(res[0]["reason"], "capability")
        self.assertFalse(self.log.exists())

    def bounded(self, seconds=2, mb=100):
        return mk("b", expect={"kind": "bounded", "time_seconds": seconds, "memory_mb": mb,
                              "accept": {"kind": "events", "events": ["+STR", "-STR"]}, "category": "limit"})

    def test_a_case_states_its_own_time_bound_whatever_the_manifest_says(self):
        a = self.adapter({"b": "hang"}, timeout=30)  # the manifest would wait half a minute
        h = a.start()
        t0 = __import__("time").monotonic()
        res = R.execute(a, set(h["capabilities"]), [self.bounded(seconds=0.5)])
        self.assertLess(__import__("time").monotonic() - t0, 5)
        self.assertEqual((res[0]["loose"], res[0]["strict"]), ("fail", "fail"))
        self.assertIn("0.5", res[0]["detail"])

    def test_measurements_are_recorded_for_bounded_cases(self):
        a = self.adapter({"b": "ok-events"})
        h = a.start()
        res = R.execute(a, set(h["capabilities"]), [self.bounded()])
        m = res[0]["measured"]
        self.assertGreaterEqual(m["seconds"], 0)
        strict = "pass" if m["peak_mb"] is not None else "unsupported"
        self.assertEqual((res[0]["loose"], res[0]["strict"]), ("pass", strict))
        self.assertEqual(res[0]["got"]["status"], "ok")

    @unittest.skipUnless(os.path.exists("/proc/self/clear_refs"), "needs Linux /proc")
    def test_memory_bound_is_enforced_while_the_case_runs(self):
        a = self.adapter({"b": "alloc-hold"}, timeout=30)
        h = a.start()
        res = R.execute(a, set(h["capabilities"]), [self.bounded(seconds=20, mb=50)])
        self.assertEqual((res[0]["loose"], res[0]["strict"]), ("fail", "fail"))
        self.assertIn("memory growth", res[0]["detail"])

    @unittest.skipUnless(os.path.exists("/proc/self/clear_refs"), "needs Linux /proc")
    def test_memory_peak_is_checked_after_a_fast_answer_too(self):
        a = self.adapter({"b": "alloc-answer"}, timeout=30)
        h = a.start()
        res = R.execute(a, set(h["capabilities"]), [self.bounded(seconds=20, mb=50)])
        self.assertEqual((res[0]["loose"], res[0]["strict"]), ("fail", "fail"))
        self.assertGreater(res[0]["measured"]["peak_mb"], 100)

    def wrapped(self, script, how):
        """The fake adapter behind a shell that does not exec it: the kit starts `sh`."""
        (self.t / "script.json").write_text(json.dumps(script))
        env = dict(os.environ, FAKE_SCRIPT=str(self.t / "script.json"), FAKE_LOG=str(self.log))
        cmd = ["sh", "-c", f"{sys.executable} {FAKE}; true"] if how == "sh" else \
            [sys.executable, "-c", f"import subprocess,sys; sys.exit(subprocess.call([sys.executable, {FAKE!r}]))"]
        a = SubprocessAdapter(cmd, env=env, timeout=30)
        self.addCleanup(a.close)
        return a

    @unittest.skipUnless(os.path.exists("/proc/self/clear_refs"), "needs Linux /proc")
    def test_memory_is_measured_for_the_whole_process_tree(self):
        for how in ("sh", "python"):
            a = self.wrapped({"b": "alloc-hold"}, how)
            h = a.start()
            res = R.execute(a, set(h["capabilities"]), [self.bounded(seconds=20, mb=50)])
            self.assertEqual((res[0]["loose"], res[0]["strict"]), ("fail", "fail"), how)
            self.assertIn("memory growth", res[0]["detail"], how)
            a2 = self.wrapped({"b": "alloc-answer"}, how)
            h = a2.start()
            res = R.execute(a2, set(h["capabilities"]), [self.bounded(seconds=20, mb=50)])
            self.assertEqual((res[0]["loose"], res[0]["strict"]), ("fail", "fail"), how)
            self.assertGreater(res[0]["measured"]["peak_mb"], 100, how)

    @unittest.skipUnless(os.path.exists("/proc/self/clear_refs"), "needs Linux /proc")
    def test_nothing_of_the_adapter_session_survives_a_failure(self):
        a = self.wrapped({"b": "alloc-hold"}, "sh")
        h = a.start()
        session = a.proc.pid
        R.execute(a, set(h["capabilities"]), [self.bounded(seconds=20, mb=50)])
        self.assertEqual(P.tree_pids(session), [])

    def test_emulated_behaviour_is_declared_and_recorded(self):
        a = self.adapter({"a": "emulated", "b": "emulated-undeclared"})
        a.env["FAKE_EMULATES"] = json.dumps(["unchanged"])
        h = a.start()
        self.assertEqual(h["emulates"], ["unchanged"])
        res = R.execute(a, set(h["capabilities"]), [mk("a"), mk("b")], emulates=h["emulates"])
        self.assertEqual((res[0]["loose"], res[0]["emulated"]), ("pass", ["unchanged"]))
        # A response may not claim behaviour its hello did not declare.
        self.assertEqual(res[1]["loose"], "fail")
        self.assertIn("did not declare", res[1]["detail"])

    def test_bad_hello_is_an_error(self):
        a = SubprocessAdapter([sys.executable, "-c", "print('hi')"], timeout=0.5)
        with self.assertRaises(AdapterError):
            a.start()

    def test_missing_program_is_an_error(self):
        with self.assertRaises(AdapterError):
            SubprocessAdapter(["/nonexistent/adapter"]).start()

    def test_input_bytes_arrive_exactly(self):
        code = (
            "import sys, json, base64\n"
            "HELLO = {'type':'hello','protocol':1,'name':'e','version':'0','language':'py','capabilities':['events']}\n"
            "for l in sys.stdin:\n"
            " r = json.loads(l)\n"
            " if r['type']=='hello': print(json.dumps(HELLO), flush=True)\n"
            " elif r['type']=='run': print(json.dumps(\n"
            "  {'id':r['id'],'status':'ok','events':[base64.b64decode(r['input']).hex()]}), flush=True)\n"
            " else: break\n")
        a = SubprocessAdapter([sys.executable, "-c", code], timeout=2)
        self.addCleanup(a.close)
        a.start()
        data = b"\xef\xbb\xbfa: \xff\r\n\x00\xfe"
        self.assertEqual(a.run("x", "events", data, {})["events"], [data.hex()])


def result(cid, loose="pass", strict="pass", reason=None, corrected=None):
    r = {"id": cid, "loose": loose, "strict": strict, "detail": "d"}
    if reason:
        r["reason"] = reason
    if corrected:
        r["corrected"] = corrected
    return r


def dev(cid, mode="strict", verdict="fail", expectation="primary"):
    return {"case": cid, "mode": mode, "verdict": verdict, "expectation": expectation, "reason": "r"}


class Deviations(unittest.TestCase):
    def check(self, results, devs, complete=True):
        return R.verify_deviations(results, devs, complete)

    def test_listed_failure_is_fine(self):
        v = self.check([result("a", strict="fail")], [dev("a")])
        self.assertTrue(v["ok"])

    def test_unlisted_failure_fails_the_run(self):
        v = self.check([result("a", strict="fail")], [])
        self.assertFalse(v["ok"])
        self.assertEqual(len(v["unlisted"]), 1)

    def test_stale_entry_fails_the_run(self):
        v = self.check([result("a")], [dev("a")])
        self.assertFalse(v["ok"])
        self.assertIn("now pass", v["stale"][0])

    def test_entry_with_the_wrong_verdict_is_stale(self):
        v = self.check([result("a", loose="unsupported", strict="unsupported", reason="adapter")],
                       [dev("a", "loose", "fail"), dev("a", "strict", "unsupported")])
        self.assertEqual(len(v["stale"]), 1)

    def test_every_mode_needs_its_own_entry(self):
        v = self.check([result("a", loose="fail", strict="fail")], [dev("a", "strict")])
        self.assertEqual(len(v["unlisted"]), 1)

    def test_unknown_case_fails_only_in_a_complete_run(self):
        self.assertFalse(self.check([result("a")], [dev("zz")], complete=True)["ok"])
        self.assertTrue(self.check([result("a")], [dev("zz")], complete=False)["ok"])

    def test_capability_unsupported_needs_no_entry_and_a_listed_one_is_stale(self):
        r = result("a", loose="unsupported", strict="unsupported", reason="capability")
        self.assertTrue(self.check([r], [])["ok"])
        self.assertFalse(self.check([r], [dev("a", "strict", "unsupported")])["ok"])

    def test_adapter_declined_case_must_be_listed(self):
        r = result("a", loose="unsupported", strict="unsupported", reason="adapter")
        self.assertFalse(self.check([r], [])["ok"])
        self.assertTrue(self.check([r], [dev("a", "loose", "unsupported"), dev("a", "strict", "unsupported")])["ok"])

    def test_corrected_verdicts_are_listed_separately(self):
        c = {"loose": "pass", "strict": "fail", "detail": ""}
        r = result("a", strict="fail", corrected=c)
        self.assertEqual(len(self.check([r], [dev("a")])["unlisted"]), 1)
        self.assertTrue(self.check([r], [dev("a"), dev("a", expectation="corrected")])["ok"])
        # A corrected entry for a case without a correction is stale.
        self.assertFalse(self.check([result("b", strict="fail")], [dev("b"), dev("b", expectation="corrected")])["ok"])

    def test_deviation_file_is_validated(self):
        t = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, t)

        def load(entries):
            (t / "d.json").write_text(json.dumps({"format": R.DEVIATIONS_FORMAT, "entries": entries}))
            return R.load_deviations({"_dir": t, "deviations": "d.json"})

        self.assertEqual(len(load([dev("a")])), 1)
        with self.assertRaises(R.RunError):
            load([dev("a"), dev("a")])
        with self.assertRaises(R.RunError):
            load([{**dev("a"), "reason": ""}])
        with self.assertRaises(R.RunError):
            load([{**dev("a"), "mode": "medium"}])
        with self.assertRaises(R.RunError):
            load([{**dev("a"), "verdict": "pass"}])
        with self.assertRaises(R.RunError):
            load([{**dev("a"), "extra": 1}])


class ResultFormat(unittest.TestCase):
    def test_validate(self):
        good = {"format": R.RESULT_FORMAT, "kit": {}, "run": {}, "deviations": {},
                "adapter": {"name": "n", "version": "v", "language": "l", "capabilities": [], "protocol": 1},
                "cases": [{"id": "a", "suite": "s", "category": "c", "op": "events", "expect": "events",
                           "loose": "pass", "strict": "pass"}]}
        self.assertEqual(R.validate_result(good), [])
        bad = json.loads(json.dumps(good))
        bad["cases"][0]["loose"] = "maybe"
        self.assertTrue(R.validate_result(bad))
        bad = json.loads(json.dumps(good))
        bad["cases"].append(bad["cases"][0])
        self.assertTrue(R.validate_result(bad))
        self.assertTrue(R.validate_result({"format": "x"}))


if __name__ == "__main__":
    unittest.main()
