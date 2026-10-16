import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def bounded(self, seconds=2, mb=100, cid="b"):
        return mk(cid, expect={"kind": "bounded", "time_seconds": seconds, "memory_mb": mb,
                              "accept": {"kind": "events", "events": ["+STR", "-STR"]}, "category": "limit"})

    def test_a_bounded_case_runs_in_a_process_of_its_own(self):
        pidlog = self.t / "pids"
        a = self.adapter({})
        a.env["FAKE_PIDLOG"] = str(pidlog)
        h = a.start()
        cases = [mk("a"), self.bounded(cid="first"), mk("c"), self.bounded(cid="second")]
        res = R.execute(a, set(h["capabilities"]), cases)
        self.assertEqual([r["loose"] for r in res], ["pass"] * 4)
        pid = {f[0]: f[1] for f in (line.split() for line in pidlog.read_text().splitlines())}
        self.assertEqual(pid["a"], pid["c"])  # ordinary cases share one session
        self.assertEqual(len({pid["a"], pid["first"], pid["second"]}), 3)
        self.assertEqual(a.fresh_sessions, 2)
        self.assertEqual(a.restarts, 0)

    @unittest.skipUnless(os.path.exists("/proc/self/clear_refs"), "needs Linux /proc")
    def test_the_memory_of_a_bounded_case_does_not_depend_on_earlier_cases(self):
        # The adapter fills a pool on its first such case and reuses it afterwards, as a
        # runtime that keeps freed memory does. In one shared session the second case would
        # show no growth; each case gets its own process, so both show the cost.
        a = self.adapter({"first": "pool", "second": "pool"}, timeout=30)
        h = a.start()
        res = R.execute(a, set(h["capabilities"]),
                        [self.bounded(seconds=20, mb=500, cid="first"), self.bounded(seconds=20, mb=50, cid="second")])
        self.assertEqual(res[0]["loose"], "pass")
        self.assertEqual((res[1]["loose"], res[1]["strict"]), ("fail", "fail"))
        self.assertGreater(res[1]["measured"]["peak_mb"], 100)

    def test_a_fresh_session_that_cannot_start_fails_that_case_only(self):
        a = self.adapter({})
        h = a.start()
        caps = set(h["capabilities"])
        # Answers something that is not a hello.
        a.command = [sys.executable, "-c", "print('not a hello')"]
        res = R.execute(a, caps, [self.bounded(cid="first"), mk("a")])
        self.assertEqual((res[0]["loose"], res[1]["loose"]), ("fail", "pass"))
        self.assertIn("did not start", res[0]["detail"])
        # Dies before it reads the hello: writing the hello fails. Run it for real a few times,
        # because whether the write or the read notices first is a race, and once on purpose.
        for cmd in (["/bin/true"], ["/bin/sh", "-c", "exec 0<&- 1>&-"]):
            a.command = cmd
            for _ in range(10):
                res = R.execute(a, caps, [self.bounded(cid="first"), mk("a")])
                self.assertEqual((res[0]["loose"], res[1]["loose"]), ("fail", "pass"), cmd)
                self.assertIn("did not start", res[0]["detail"])
        a.command = [sys.executable, FAKE]
        real_send = SubprocessAdapter._send

        def broken_pipe_on_hello(self_, obj):
            if obj.get("type") == "hello":
                raise BrokenPipeError(32, "Broken pipe")
            return real_send(self_, obj)

        with mock.patch.object(SubprocessAdapter, "_send", broken_pipe_on_hello):
            res = R.execute(a, caps, [self.bounded(cid="first"), mk("a")])
        self.assertEqual((res[0]["loose"], res[1]["loose"]), ("fail", "pass"))
        self.assertIn("did not start", res[0]["detail"])
        # The process cannot be launched at all (a failing pre-exec step raises SubprocessError).
        with mock.patch.object(P.subprocess, "Popen", side_effect=P.subprocess.SubprocessError("preexec")):
            res = R.execute(a, caps, [self.bounded(cid="first"), mk("a")])
        self.assertEqual((res[0]["loose"], res[1]["loose"]), ("fail", "pass"))
        self.assertIn("did not start", res[0]["detail"])

    def test_a_fresh_session_answers_a_trivial_request_before_the_case(self):
        pidlog = self.t / "pids"
        a = self.adapter({})
        a.env["FAKE_PIDLOG"] = str(pidlog)
        h = a.start()
        R.execute(a, set(h["capabilities"]), [self.bounded(cid="first")])
        rows = [line.split() for line in pidlog.read_text().splitlines()]
        self.assertEqual([r[0] for r in rows], ["warm-up", "first"])
        self.assertEqual(rows[0][1], rows[1][1])  # the same process serves both

    @unittest.skipUnless(os.path.exists("/proc/self/clear_refs"), "needs Linux /proc")
    def test_first_use_cost_is_not_counted_against_a_bounded_case(self):
        # The adapter fills its pool on the first request of that kind. Done by the trivial
        # request, the case itself shows no growth; done by the case, it would show 200 MB.
        a = self.adapter({"warm-up": "pool", "first": "pool"}, timeout=30)
        h = a.start()
        res = R.execute(a, set(h["capabilities"]), [self.bounded(seconds=20, mb=50, cid="first")])
        self.assertEqual(res[0]["loose"], "pass")
        self.assertLess(res[0]["measured"]["peak_mb"], 50)

    def test_a_fresh_session_that_dies_on_the_trivial_request_fails_that_case_only(self):
        a = self.adapter({"warm-up": "crash"})
        h = a.start()
        res = R.execute(a, set(h["capabilities"]), [self.bounded(cid="first"), mk("a")])
        self.assertEqual((res[0]["loose"], res[1]["loose"]), ("fail", "pass"))
        self.assertIn("warm-up", res[0]["detail"])

    def test_a_failed_restart_fails_that_case_and_the_run_goes_on(self):
        a = self.adapter({"b": "crash"}, timeout=1)
        h = a.start()
        caps = set(h["capabilities"])
        startlog = self.t / "starts"
        good = a.command
        # Every start of the replacement answers something that is not a hello.
        a.command = [sys.executable, "-c",
                     f"open({str(startlog)!r}, 'a').write('x'); print('not a hello')"]
        res = R.execute(a, caps, [mk("b"), mk("c"), mk("d"), mk("e"), mk("f"), mk("g")])
        self.assertEqual([r["loose"] for r in res], ["fail"] * 6)
        self.assertIn("exited", res[0]["detail"])
        for r in res[1:4]:
            self.assertIn("could not be restarted", r["detail"])
        # The restart is attempted for each of the next cases, three in a row, and then the rest of
        # the run is refused at once instead of paying a start-up timeout for every case.
        self.assertEqual(startlog.read_text(), "xxx")
        for r in res[4:]:
            self.assertIn("not running", r["detail"])
        self.assertEqual(a.restarts, 0)
        a.command = good
        a.failed_starts = 0

    def started_adapter(self, later, script=None, timeout=1):
        """An adapter whose first start is accepted as the run's hello and whose later starts say `later`."""
        a = self.adapter(script or {"b": "crash"}, timeout=timeout)
        a.env["FAKE_STARTS"] = str(self.t / "fake-starts")
        a.env["FAKE_LATER"] = json.dumps(later)
        h = a.start()
        a.expected_hello = h  # what the runner does once it has accepted the first hello
        return a, set(h["capabilities"])

    def test_a_restart_that_says_something_else_fails_the_case_and_the_rest(self):
        for later, word in (({"capabilities": ["events", "value", "outcome:value"]}, "capabilities"),
                            ({"capabilities": ["events"]}, "capabilities"),
                            ({"protocol": 2}, "protocol"),
                            ({"version": "1"}, "version"),
                            ({"language": "go"}, "language"),
                            ({"name": "other"}, "name"),
                            ({"emulates": ["unchanged"]}, "emulates")):
            with self.subTest(later=later):
                self.t = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
                self.addCleanup(shutil.rmtree, self.t)
                a, caps = self.started_adapter(later)
                res = R.execute(a, caps, [mk("b"), mk("c"), mk("d"), mk("e"), mk("f")])
                # The case that crashed, then every later case: none runs against the new hello.
                self.assertEqual([r["loose"] for r in res], ["fail"] * 5)
                self.assertIn("exited", res[0]["detail"])
                for r in res[1:4]:
                    self.assertIn("could not be restarted", r["detail"])
                    self.assertIn("hello differs from the first start", r["detail"])
                    self.assertIn(word, r["detail"])
                self.assertIn("not running", res[4]["detail"])
                self.assertEqual(a.restarts, 0)

    def test_a_restart_with_the_same_hello_goes_on(self):
        a, caps = self.started_adapter({"capabilities": ["value", "events"]})  # the same set, another order
        res = R.execute(a, caps, [mk("b"), mk("c")])
        self.assertEqual([r["loose"] for r in res], ["fail", "pass"])
        self.assertEqual(a.restarts, 1)

    def test_a_bounded_case_in_a_process_that_says_something_else_fails_that_case_only(self):
        a, caps = self.started_adapter({"version": "1"}, script={})
        res = R.execute(a, caps, [self.bounded(), mk("a")])
        self.assertEqual(res[0]["loose"], "fail")
        self.assertIn("hello differs from the first start", res[0]["detail"])
        self.assertEqual(res[1]["loose"], "pass")

    def test_a_later_restart_can_succeed_after_a_failed_one(self):
        a = self.adapter({"b": "crash"}, timeout=1)
        h = a.start()
        caps = set(h["capabilities"])
        good = a.command
        a.command = [sys.executable, "-c", "print('not a hello')"]
        res = R.execute(a, caps, [mk("b"), mk("c")])
        self.assertEqual([r["loose"] for r in res], ["fail", "fail"])
        a.command = good
        res = R.execute(a, caps, [mk("d"), mk("e")])
        self.assertEqual([r["loose"] for r in res], ["pass", "pass"])
        self.assertEqual((a.restarts, a.failed_starts), (1, 0))

    def test_a_bounded_case_after_a_failed_restart_still_gets_its_own_process(self):
        a = self.adapter({"b": "crash"}, timeout=1)
        h = a.start()
        caps = set(h["capabilities"])
        good = a.command
        a.command = [sys.executable, "-c", "print('not a hello')"]
        R.execute(a, caps, [mk("b")])
        a.command = good
        res = R.execute(a, caps, [self.bounded(cid="first")])
        self.assertEqual(res[0]["loose"], "pass")

    def test_a_case_states_its_own_time_bound_whatever_the_manifest_says(self):
        a = self.adapter({"b": "hang"}, timeout=30)  # the manifest would wait half a minute
        h = a.start()
        waits = []
        real = SubprocessAdapter._recv

        def record(adapter, timeout, *args, **kw):
            waits.append(timeout)
            return real(adapter, timeout, *args, **kw)

        # Check which wait the case was given rather than how long the run took.
        with mock.patch.object(SubprocessAdapter, "_recv", record):
            res = R.execute(a, set(h["capabilities"]), [self.bounded(seconds=0.5)])
        # The last wait is the one for the case's answer; the earlier ones are the start-up of its process.
        self.assertEqual(waits[-1], 0.5)
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
        # A bounded case runs in a session of its own; when it fails, all of that session goes.
        pidlog = self.t / "pids"
        a = self.wrapped({"b": "alloc-hold"}, "sh")
        a.env["FAKE_PIDLOG"] = str(pidlog)
        h = a.start()
        res = R.execute(a, set(h["capabilities"]), [self.bounded(seconds=20, mb=50)])
        self.assertEqual(res[0]["loose"], "fail")
        _, pid, session = pidlog.read_text().splitlines()[-1].split()
        self.assertNotEqual(int(session), a.proc.pid)  # not the shared session
        self.assertEqual(P.tree_pids(int(session)), [])

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

    def test_an_entry_for_a_deferred_slow_case_is_kept_but_a_real_unknown_one_still_fails(self):
        done = [result("a")]
        v = R.verify_deviations(done, [dev("slow")], True, deferred={"slow"})
        self.assertTrue(v["ok"])
        v = R.verify_deviations(done, [dev("slow"), dev("zz")], True, deferred={"slow"})
        self.assertEqual(len(v["unknown"]), 1)
        self.assertIn("zz", v["unknown"][0])

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
