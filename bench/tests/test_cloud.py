"""Self-tests for the cloud launcher, with the AWS layer replaced by a fake.

They check the things that must hold whatever the account does: every way out of a run tears
everything down, a lost instance is retried and finally replaced by an on-demand one of the same
type, the cost cap refuses before anything starts, and a plan maps to one job per shard.
"""

import gzip
import io
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import _thread
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneyaml_bench.cloud import bootstrap, cleanup, cli, config, pack, plan, runner, sigv4, toolchains  # noqa: E402
from oneyaml_bench.cloud.aws import DEADLINE_TAG, RUN_TAG, Aws, AwsError, Instance, LaunchError, Presigner  # noqa: E402

KIT = Path(__file__).resolve().parents[2]
URL = re.compile(r"https://[^'\s]+")


def fake_result(itype="c7i.test"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = json.dumps({"run": {"environment": {"instance_type": itype}}, "cells": []}).encode()
        info = tarfile.TarInfo("0/results.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeAws:
    """An account in memory. `behave(spec, n)` decides what each launched instance does:
    "done", "bad-exit", "interrupt" (ends without a result), "hang", or ("error", code)."""

    def __init__(self, behave=lambda spec, n: "done"):
        self.behave = behave
        self.store: dict[str, bytes] = {}
        self.inst: dict[str, Instance] = {}
        self.launches: list = []
        self.groups: set[str] = set()
        self.group_tags: dict[str, tuple[str, float]] = {}      # group id -> (run id, deadline)
        self.images: list[dict] = []                            # {"image", "digest", "snapshots", "in_use"}
        self.removed_images: list[str] = []
        self.terminate_calls = 0
        self.n = 0

    # keys are what the runner fetches; the presigned URLs in the user data name them
    def presigner(self, seconds):
        return Presigner("bucket.s3.test.amazonaws.com", "test", {"AccessKeyId": "AKIAFAKE", "SecretAccessKey": "k" * 40,
                                                                   "SessionToken": "t" * 600}, seconds)

    def fetch(self, key):
        return self.store.get(key)

    def upload(self, path, key):
        self.store[key] = Path(path).read_bytes()

    def delete_prefix(self, prefix):
        gone = [k for k in self.store if k.startswith(prefix)]
        for k in gone:
            del self.store[k]
        return len(gone)

    def delete_keys(self, keys):
        for k in keys:
            self.store.pop(k, None)
        return len(keys)

    def list_keys(self, prefix, older_than=None):
        return [k for k in self.store if k.startswith(prefix)]

    def create_security_group(self, run_id, deadline=0.0):
        gid = f"sg-{run_id}"
        self.groups.add(gid)
        self.group_tags[gid] = (run_id, deadline)
        return gid

    def delete_security_group(self, gid, attempts=1):
        self.groups.discard(gid)
        return True

    def security_groups(self, run_id=None):
        return sorted(g for g in self.groups if run_id is None or self.group_tags.get(g, (run_id,))[0] == run_id)

    def run_deadlines(self):
        out = {}
        for i in self.inst.values():
            run = i.tags.get(RUN_TAG)
            if run and i.state != "terminated":
                out[run] = max(out.get(run, 0.0), i.deadline if i.deadline is not None else i.launched + 24 * 3600)
        for g in self.groups:
            run, d = self.group_tags.get(g, (None, 0.0))
            if run:
                out[run] = max(out.get(run, 0.0), d)
        return out

    def expire(self, resource_ids):
        for rid in resource_ids:
            if rid in self.inst:
                self.inst[rid].tags[DEADLINE_TAG] = str(int(time.time()) - 1)
            if rid in self.group_tags:
                self.group_tags[rid] = (self.group_tags[rid][0], time.time() - 1)

    def superseded_images(self, keep_digest, in_use=frozenset(), older_than=0.0):
        cur = [i for i in self.images if i["digest"] == keep_digest]
        keep = cur[-1]["image"] if cur else None
        return [dict(i) for i in self.images if i["image"] != keep and i["image"] not in in_use and not i.get("in_use")
                and i.get("state", "available") == "available" and i.get("created", 0.0) < older_than]

    def remove_image(self, image_id, snapshots):
        self.removed_images.append(image_id)
        self.images = [i for i in self.images if i["image"] != image_id]

    def launch_templates(self, run_id=None):
        return []

    def volumes(self, run_id=None):
        return []

    def spot_requests(self, run_id=None):
        return []

    def cancel_spot_requests(self, run_id=None):
        return 0

    def launch(self, spec):
        self.n += 1
        n = self.n
        action = self.behave(spec, n)
        self.launches.append(spec)
        if isinstance(action, tuple):
            raise LaunchError(action[1], "fake")
        iid = f"i-{n:04d}"
        tags = {RUN_TAG: spec.run_id}
        if spec.deadline:
            tags[DEADLINE_TAG] = str(int(spec.deadline))
        self.inst[iid] = Instance(iid, "running", spec.type, "spot" if spec.spot else "on-demand",
                                  spec.zone or "test-a", time.time(), tags=tags, image=spec.image)
        script = gzip.decompress(spec.user_data).decode()
        urls = {u.split("?")[0].rsplit("/", 1)[1]: u.split("?")[0].split(".com/", 1)[1] for u in URL.findall(script) if ".amazonaws.com/" in u}
        def finish(state):
            now = time.time()
            self.store[urls["log.txt"]] = b"== run\nmeasuring (quick)\n"
            self.store[urls["result.tar.gz"]] = fake_result(spec.type)
            self.store[urls["status.json"]] = json.dumps({
                "done": True, "exit": 3 if action == "bad-exit" else 0, "governor": "performance",
                "marks": {"boot": now - 30, "script_start": now - 25, "setup_done": now - 20, "inputs_ready": now - 15,
                          "cache_warm": now - 14, "bench_start": now - 13, "measuring": now - 10, "bench_done": now - 1,
                          "finished": now},
                "instance": {"KIT_INSTANCE_TYPE": spec.type, "KIT_INSTANCE_AZ": "test-a", "KIT_INSTANCE_LIFECYCLE": "spot"}}).encode()
            self.inst[iid].state = state
        if action in ("done", "bad-exit", "done-slow"):
            finish("shutting-down" if action == "done-slow" else "terminated")
        elif action == "done-late":
            # The result appears only after the instance listing has been blind to the instance for a while.
            threading.Timer(1.8, finish, ["terminated"]).start()
        elif action == "interrupt":
            self.inst[iid].state = "terminated"
            self.inst[iid].reason = "Server.SpotInstanceTermination"
        return iid

    def instances(self, run_id=None):
        return [Instance(i.id, i.state, i.type, i.lifecycle, i.az, i.launched, reason=i.reason, tags=dict(i.tags), image=i.image)
                for i in self.inst.values() if run_id is None or i.tags.get(RUN_TAG, run_id) == run_id]

    def terminate(self, ids):
        self.terminate_calls += 1
        for i in ids:
            self.inst[i].state = "terminated"

    def finish_terminations(self):
        for i in self.inst.values():
            if i.state == "shutting-down":
                i.state = "terminated"

    def running(self):
        return [i for i in self.inst.values() if i.state != "terminated"]

    def clean(self):
        return not self.running() and not self.groups and not [k for k in self.store if k.startswith("runs/")]


def job_list(n=2, itype="c7i.test", minutes=5):
    return [runner.Job(i, f"{i}/{n}", itype, [["bench", "run", "--shard", f"{i}/{n}", "--out", "{out}"]], minutes, 60.0) for i in range(1, n + 1)]


class RunCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        # A background job starts with SIGINT ignored, which would make the simulated Ctrl-C do nothing.
        signal.signal(signal.SIGINT, signal.default_int_handler)
        (self.dir / "k.tar.gz").write_bytes(b"x")
        self.packs = [pack.Packed("kit", self.dir / "k.tar.gz", "a" * 40, False, "git", "0" * 64, 1)]
        self.lines: list[str] = []

    def tearDown(self):
        for s, h in self.saved.items():
            signal.signal(s, h)
        self.tmp.cleanup()

    def make(self, aws, jobs=None, cfg=None, **opts):
        o = runner.Options(out=self.dir / "out", poll_seconds=0.02, launch_backoff_seconds=0, **opts)
        r = runner.Run(aws, cfg or config.Config("test", "bucket", "ami-x"), o, jobs or job_list(), "ami-image", self.packs, 0.5, 1.5,
                       say=self.lines.append, fetch=aws.fetch, run_id="rtest")
        r.teardown.poll = 0.01
        return r


class Teardown(RunCase):
    def test_success_leaves_nothing(self):
        aws = FakeAws()
        s = self.make(aws).execute()
        self.assertTrue(s["ok"])
        self.assertTrue(aws.clean(), (aws.inst, aws.groups, list(aws.store)))
        self.assertTrue((self.dir / "out" / "cloud-run.json").exists())
        self.assertTrue((self.dir / "out" / "jobs" / "1" / "0" / "results.json").exists())

    def test_failing_job_still_tears_down(self):
        aws = FakeAws(lambda spec, n: "bad-exit")
        with self.assertRaises(runner.RunFailed):
            self.make(aws).execute()
        self.assertTrue(aws.clean())

    def test_hung_instance_is_terminated_on_ctrl_c(self):
        aws = FakeAws(lambda spec, n: "hang")
        threading.Timer(0.4, _thread.interrupt_main).start()
        with self.assertRaises(KeyboardInterrupt):
            self.make(aws).execute()
        self.assertEqual(len(aws.launches), 2)
        self.assertTrue(aws.clean())

    def test_hung_instance_is_terminated_on_sigterm(self):
        aws = FakeAws(lambda spec, n: "hang")
        runner.install_signal_handlers()
        threading.Timer(0.4, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        with self.assertRaises(KeyboardInterrupt):
            self.make(aws).execute()
        self.assertTrue(aws.clean())

    def test_time_limit_terminates(self):
        aws = FakeAws(lambda spec, n: "hang")
        r = self.make(aws, timeout_minutes=0)
        with self.assertRaises(runner.RunFailed):
            r.execute()
        self.assertTrue(aws.clean())

    def test_error_while_launching_tears_down(self):
        aws = FakeAws(lambda spec, n: "done" if n == 1 else ("error", "UnauthorizedOperation"))
        with self.assertRaises(runner.RunFailed):
            self.make(aws).execute()
        self.assertTrue(aws.clean())

    def test_crash_in_setup_tears_down(self):
        aws = FakeAws()
        aws.upload = lambda path, key: (_ for _ in ()).throw(RuntimeError("upload broke"))
        with self.assertRaises(RuntimeError):
            self.make(aws).execute()
        self.assertTrue(aws.clean())

    def test_leftover_instance_is_reported_not_hidden(self):
        aws = FakeAws()
        aws.terminate = lambda ids: None    # an account that ignores the request
        aws.behave = lambda spec, n: "hang"
        r = self.make(aws, jobs=job_list(1))
        r.teardown.wait_seconds = 0.1
        threading.Timer(0.3, _thread.interrupt_main).start()
        with self.assertRaises(KeyboardInterrupt):
            r.execute()
        summary = json.loads((self.dir / "out" / "cloud-run.json").read_text())
        self.assertFalse(summary["ok"])
        self.assertTrue(summary["teardown_problems"])


class SlowTermination(RunCase):
    def test_instances_still_shutting_down_are_pending_not_failures(self):
        aws = FakeAws(lambda spec, n: "done-slow")
        r = self.make(aws, jobs=job_list(1))
        r.teardown.wait_seconds = 0.05
        s = r.execute()
        self.assertTrue(s["ok"])
        self.assertTrue(s["teardown_pending"])
        self.assertEqual(cleanup.still_running(cleanup.inventory(aws)), [])
        self.assertTrue(aws.groups)         # held back until the instance is gone
        aws.finish_terminations()
        left = cleanup.cleanup(aws, "rtest", lambda m: None, wait=0)
        self.assertEqual(cleanup.total(left), 0)
        self.assertTrue(aws.clean())

    def test_a_running_instance_is_a_failure(self):
        aws = FakeAws(lambda spec, n: "hang")
        aws.terminate = lambda ids: None
        r = self.make(aws, jobs=job_list(1), timeout_minutes=0)
        r.teardown.wait_seconds = 0.05
        with self.assertRaises(runner.RunFailed):
            r.execute()
        self.assertTrue(json.loads((self.dir / "out" / "cloud-run.json").read_text())["teardown_problems"])

    def test_merge_hook_runs_and_its_failure_does_not_skip_teardown(self):
        aws = FakeAws()
        seen = []

        def hook(run):
            seen.append(len(aws.running()))
            raise RuntimeError("merge broke")
        r = self.make(aws, jobs=job_list(1))
        r.after_collect = hook
        s = r.execute()
        self.assertEqual(seen, [0])
        self.assertIn("merge broke", s["extra"]["after_collect_error"])
        self.assertTrue(aws.clean())


class RetryAndFallback(RunCase):
    def test_lost_spot_instance_is_retried_on_spot(self):
        aws = FakeAws(lambda spec, n: "interrupt" if n == 1 else "done")
        s = self.make(aws, jobs=job_list(1)).execute()
        job = s["jobs"][0]
        self.assertEqual([a["outcome"] for a in job["attempts"]], ["lost", "done"])
        self.assertEqual([a["lifecycle"] for a in job["attempts"]], ["spot", "spot"])
        self.assertFalse(job["ran_on_demand"])
        self.assertTrue(aws.clean())

    def test_capacity_failures_fall_back_to_on_demand_and_say_so(self):
        aws = FakeAws(lambda spec, n: ("error", "InsufficientInstanceCapacity") if spec.spot else "done")
        s = self.make(aws, jobs=job_list(1)).execute()
        job = s["jobs"][0]
        self.assertTrue(job["ran_on_demand"])
        self.assertEqual(len([a for a in job["attempts"] if a["lifecycle"] == "spot"]), 3)
        self.assertEqual({sp.type for sp in aws.launches}, {"c7i.test"})
        self.assertTrue(aws.clean())

    def test_bounded_retries_then_failure(self):
        aws = FakeAws(lambda spec, n: "interrupt")
        with self.assertRaises(runner.RunFailed):
            self.make(aws, jobs=job_list(1)).execute()
        self.assertEqual(len(aws.launches), 4)      # three spot attempts, one on-demand
        self.assertTrue(aws.clean())

    def test_no_fallback_when_disabled(self):
        aws = FakeAws(lambda spec, n: ("error", "InsufficientInstanceCapacity"))
        with self.assertRaises(runner.RunFailed):
            self.make(aws, jobs=job_list(1), on_demand_fallback=False).execute()
        self.assertFalse([s for s in aws.launches if not s.spot])

    def test_fallback_refused_above_the_cost_cap(self):
        aws = FakeAws(lambda spec, n: ("error", "InsufficientInstanceCapacity") if spec.spot else "done")
        with self.assertRaises(runner.RunFailed):
            self.make(aws, jobs=job_list(1), cost_cap=0.0001).execute()
        self.assertFalse([s for s in aws.launches if not s.spot])

    def test_output_of_a_failed_job_is_kept_but_not_marked_ok(self):
        aws = FakeAws(lambda spec, n: "bad-exit")
        r = self.make(aws, jobs=job_list(1))
        with self.assertRaises(runner.RunFailed):
            r.execute()
        res = r.results[1]
        self.assertFalse(res.ok)
        self.assertIsNone(res.out_dir)
        self.assertTrue((res.partial_dir / "0" / "results.json").exists())

    def test_real_failure_is_not_retried(self):
        aws = FakeAws(lambda spec, n: "bad-exit")
        with self.assertRaises(runner.RunFailed):
            self.make(aws, jobs=job_list(1)).execute()
        self.assertEqual(len(aws.launches), 1)

    def test_instances_never_mix_types_and_retries_use_fresh_keys(self):
        aws = FakeAws(lambda spec, n: "interrupt" if n <= 2 else "done")
        self.make(aws, jobs=job_list(2)).execute()
        self.assertEqual({s.type for s in aws.launches}, {"c7i.test"})
        scripts = [gzip.decompress(s.user_data).decode() for s in aws.launches]
        logs = [re.search(r"runs/\S+?/log.txt", x).group(0) for x in scripts]
        self.assertEqual(len(set(logs)), len(logs))


class CostCap(RunCase):
    """The cap bounds what is committed across all jobs and attempts, not each launch alone."""

    def test_on_demand_fallbacks_are_counted_together(self):
        # The on-demand instances stay alive for a moment, so each still counts for its projected cost.
        aws = FakeAws(lambda spec, n: ("error", "InsufficientInstanceCapacity") if spec.spot else "done-late")
        jobs = [runner.Job(i, f"{i}/4", "c7i.test", [["bench", "run"]], 200, 7200.0) for i in range(1, 5)]
        # An on-demand instance for two hours is $3.00 here: a cap of $7 admits two of the four.
        with self.assertRaises(runner.RunFailed):
            self.make(aws, jobs=jobs, cost_cap=7.0).execute()
        self.assertEqual(len([s for s in aws.launches if not s.spot]), 2)

    def test_spot_retries_are_checked_too(self):
        aws = FakeAws()
        jobs = [runner.Job(i, f"{i}/3", "c7i.test", [["bench", "run"]], 200, 7200.0) for i in range(1, 4)]
        r = self.make(aws, jobs=jobs, cost_cap=2.5)         # a live spot instance commits $1.00 for its two hours
        attempts = []
        for j in jobs:
            att = runner.Attempt(1, "spot")
            r.results[j.index] = runner.JobResult(j, [att])
            attempts.append((j, att))
        reasons = [r.reserve(j, a) for j, a in attempts]
        self.assertEqual([bool(x) for x in reasons], [False, False, True])
        self.assertIn("above the cap", reasons[2])

    def test_a_live_instance_counts_what_it_has_cost_when_that_is_more(self):
        r = self.make(FakeAws(), jobs=job_list(1))
        att = runner.Attempt(1, "spot", instance_id="i-1", launched=1000.0, reserved=True, rate=1.0, est_seconds=60.0)
        r.clock = lambda: 1000.0 + 7200      # it has run two hours although only a minute was projected
        self.assertAlmostEqual(r.committed(att), 2.0)
        att.ended = 1000.0 + 600
        self.assertAlmostEqual(r.committed(att), 600 / 3600)

    def test_unknown_on_demand_price_means_no_fallback(self):
        aws = FakeAws(lambda spec, n: ("error", "InsufficientInstanceCapacity") if spec.spot else "done")
        r = self.make(aws, jobs=job_list(1))
        r.on_demand_price = None
        with self.assertRaises(runner.RunFailed):
            r.execute()
        self.assertFalse([s for s in aws.launches if not s.spot])
        self.assertTrue(any("on-demand price is not known" in ln for ln in self.lines))

    def test_summary_reports_the_spot_and_on_demand_split(self):
        aws = FakeAws(lambda spec, n: ("error", "InsufficientInstanceCapacity") if spec.spot else "done")
        s = self.make(aws, jobs=job_list(2)).execute()
        self.assertEqual(s["instances_started"], {"spot": 0, "on-demand": 2})
        self.assertEqual(s["cost_by_lifecycle"]["spot"], 0)
        self.assertGreater(s["cost_by_lifecycle"]["on-demand"], 0)

    def test_estimate_uses_the_dearest_zone_and_the_volume(self):
        history = {"SpotPriceHistory": [
            {"AvailabilityZone": "z-a", "SpotPrice": "0.30"}, {"AvailabilityZone": "z-b", "SpotPrice": "0.90"},
            {"AvailabilityZone": "z-a", "SpotPrice": "0.10"}]}      # an older quote for z-a is ignored
        fake = lambda args, stdin: json.dumps(history)             # noqa: E731
        self.assertEqual(Aws(config.Config("r", "b", "a"), runner=fake).spot_price("t"), 0.90)
        only_a = config.Config("r", "b", "a", zones=("z-a",))
        self.assertEqual(Aws(only_a, runner=fake).spot_price("t"), 0.30)
        self.assertGreater(plan.volume_per_hour(40), 0.1)


class Zones(RunCase):
    def test_spot_attempts_walk_the_configured_zones_and_on_demand_takes_any(self):
        aws = FakeAws(lambda spec, n: ("error", "InsufficientInstanceCapacity") if spec.spot else "done")
        cfg = config.Config("test", "bucket", "ami-x", zones=("z-a", "z-b"))
        self.make(aws, jobs=job_list(1), cfg=cfg).execute()
        self.assertEqual([sp.zone for sp in aws.launches], ["z-a", "z-b", "z-a", ""])


class Deadlines(RunCase):
    def test_everything_a_run_creates_carries_a_deadline(self):
        aws = FakeAws()
        before = time.time()
        self.make(aws, jobs=job_list(1)).execute()
        self.assertGreater(aws.launches[0].deadline, before + 5 * 60)
        self.assertGreater(aws.group_tags["sg-rtest"][1], before + 30 * 60)

    def test_cleanup_of_all_runs_leaves_a_run_inside_its_deadline_alone(self):
        aws = FakeAws()
        now = time.time()
        aws.inst["i-young"] = Instance("i-young", "running", "t", launched=now - 60,
                                       tags={RUN_TAG: "young", DEADLINE_TAG: str(int(now + 3000))})
        aws.inst["i-old"] = Instance("i-old", "running", "t", launched=now - 9000,
                                     tags={RUN_TAG: "old", DEADLINE_TAG: str(int(now - 100))})
        aws.groups |= {"sg-young", "sg-old"}
        aws.group_tags.update({"sg-young": ("young", now + 3000), "sg-old": ("old", now - 100)})
        aws.store["runs/young/inputs.tar"] = b"x"
        lines = []
        left = cleanup.cleanup(aws, None, lines.append, wait=0)
        self.assertEqual(aws.inst["i-old"].state, "terminated")
        self.assertEqual(aws.inst["i-young"].state, "running")
        self.assertIn("sg-young", aws.groups)
        self.assertNotIn("sg-old", aws.groups)
        self.assertIn("runs/young/inputs.tar", aws.store)
        self.assertEqual(left["left_alone"], ["young"])
        self.assertTrue(any("left alone: run young" in ln for ln in lines))

    def test_a_run_that_leaves_instances_shutting_down_marks_them_due_for_the_next_cleanup(self):
        aws = FakeAws(lambda spec, n: "done-slow")          # ends in "shutting-down", which takes a while
        r = self.make(aws, jobs=job_list(1))
        r.teardown.wait_seconds = 0.2
        r.execute()
        self.assertEqual([i.state for i in aws.inst.values()], ["shutting-down"])
        self.assertIn("sg-rtest", aws.groups)               # held by the instance
        aws.finish_terminations()
        left = cleanup.cleanup(aws, None, lambda m: None, wait=0)
        self.assertNotIn("left_alone", left)
        self.assertNotIn("sg-rtest", aws.groups)

    def test_cleanup_of_a_named_run_ignores_the_deadline(self):
        aws = FakeAws()
        now = time.time()
        aws.inst["i-young"] = Instance("i-young", "running", "t", launched=now - 60,
                                       tags={RUN_TAG: "young", DEADLINE_TAG: str(int(now + 3000))})
        cleanup.cleanup(aws, "young", lambda m: None, wait=0)
        self.assertEqual(aws.inst["i-young"].state, "terminated")

    def test_an_instance_without_a_deadline_tag_is_trusted_for_a_day(self):
        aws = FakeAws()
        aws.inst["i-1"] = Instance("i-1", "running", "t", launched=time.time() - 60, tags={RUN_TAG: "legacy"})
        cleanup.cleanup(aws, None, lambda m: None, wait=0)
        self.assertEqual(aws.inst["i-1"].state, "running")

    def test_superseded_images_go_and_the_current_and_used_ones_stay(self):
        aws = FakeAws()
        aws.images = [{"image": "ami-old", "digest": "d1", "snapshots": ["snap-1"]},
                      {"image": "ami-busy", "digest": "d1", "snapshots": [], "in_use": True},
                      {"image": "ami-now", "digest": "d2", "snapshots": ["snap-2"]}]
        cleanup.cleanup(aws, "someone", lambda m: None, wait=0, keep_image_digest="d2")
        self.assertEqual(aws.removed_images, [], "a named run's cleanup never touches images")
        cleanup.cleanup(aws, None, lambda m: None, wait=0)
        self.assertEqual(aws.removed_images, [], "images are removed only on request")
        cleanup.cleanup(aws, None, lambda m: None, wait=0, keep_image_digest="d2")
        self.assertEqual(aws.removed_images, ["ami-old"])
        self.assertEqual({i["image"] for i in aws.images}, {"ami-busy", "ami-now"})


class ImageRemoval(unittest.TestCase):
    """Against the real AWS layer with canned answers: what a request for image removal may deregister."""

    def run_cleanup(self, images, now, **kw):
        calls = []

        def fake(args, stdin):
            calls.append(args)
            a = " ".join(args)
            for key, body in (("describe-instances", {"Reservations": []}), ("describe-spot-instance-requests", {"SpotInstanceRequests": []}),
                              ("describe-security-groups", {"SecurityGroups": []}), ("describe-launch-templates", {"LaunchTemplates": []}),
                              ("describe-volumes", {"Volumes": []}), ("list-objects-v2", {}), ("describe-images", {"Images": images})):
                if key in a:
                    return json.dumps(body)
            return ""
        aws = Aws(config.Config("t", "b", "ami"), runner=fake)
        cleanup.cleanup(aws, None, lambda m: None, wait=0, now=lambda: now, **kw)
        return [c[c.index("--image-id") + 1] for c in calls if "deregister-image" in c]

    def image(self, ident, digest, state, created):
        return {"ImageId": ident, "State": state, "CreationDate": created, "Tags": [{"Key": "oneyaml-kit-image", "Value": digest}],
                "BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-" + ident}}]}

    def setUp(self):
        self.now = dt_epoch("2026-10-15T12:00:00Z")
        self.images = [
            self.image("ami-mine", "D1", "available", "2026-10-15T08:00:00Z"),
            self.image("ami-other-current", "D2", "available", "2026-10-15T09:00:00Z"),      # another checkout's, in use
            self.image("ami-baking", "D3", "pending", "2026-10-15T09:30:00Z"),               # still being created
            self.image("ami-long-gone", "D4", "available", "2026-09-01T00:00:00Z"),          # older than the retention
        ]

    def test_plain_cleanup_never_touches_images(self):
        self.assertEqual(self.run_cleanup(self.images, self.now), [])

    def test_only_old_available_images_of_another_digest_go(self):
        self.assertEqual(self.run_cleanup(self.images, self.now, keep_image_digest="D1"), ["ami-long-gone"])

    def test_a_pending_image_stays_even_when_old(self):
        imgs = [self.image("ami-slow-bake", "D3", "pending", "2026-08-01T00:00:00Z")]
        self.assertEqual(self.run_cleanup(imgs, self.now, keep_image_digest="D1"), [])

    def test_the_retention_is_adjustable(self):
        got = self.run_cleanup(self.images, self.now, keep_image_digest="D1", image_retention=3600)
        self.assertEqual(sorted(got), ["ami-long-gone", "ami-other-current"])


class RunDeadlines(unittest.TestCase):
    def test_a_volume_in_use_does_not_keep_a_finished_run_young(self):
        tag = lambda run, dl=None: [{"Key": RUN_TAG, "Value": run}] + ([{"Key": DEADLINE_TAG, "Value": str(dl)}] if dl else [])    # noqa: E731

        def fake(args, stdin):
            a = " ".join(args)
            if "describe-instances" in a:
                return json.dumps({"Reservations": []})
            if "describe-security-groups" in a:
                return json.dumps({"SecurityGroups": [{"GroupId": "sg-1", "Tags": tag("done", 100)}]})
            if "describe-volumes" in a:
                return json.dumps({"Volumes": [{"State": "in-use", "Tags": tag("done", 10 ** 12)},
                                               {"State": "available", "Tags": tag("leaked", 10 ** 12)}]})
            return json.dumps({})
        got = Aws(config.Config("r", "b", "a"), runner=fake).run_deadlines()
        self.assertEqual(got, {"done": 100.0, "leaked": 0.0})


class CleanupCommand(unittest.TestCase):
    def run_command(self, argv):
        from unittest import mock
        seen = {}

        def fake_cleanup(aws, run_id, say, wait, keep_image_digest=None, **kw):
            seen["digest"] = keep_image_digest
            return {}
        with mock.patch.object(cli.cleanup_mod, "cleanup", fake_cleanup), mock.patch.object(cli.configmod, "load", lambda: config.Config("r", "b", "a")), \
                mock.patch.object(cli, "make_aws", lambda cfg: None):
            cli.main(argv)
        return seen["digest"]

    def test_images_are_left_alone_unless_asked_for(self):
        self.assertIsNone(self.run_command(["cleanup"]))
        self.assertIsNone(self.run_command(["cleanup", "--run", "one-run", "--images"]))
        self.assertIsNotNone(self.run_command(["cleanup", "--images"]))


class Signals(unittest.TestCase):
    def setUp(self):
        self.saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}

    def tearDown(self):
        for s, h in self.saved.items():
            signal.signal(s, h)

    def test_main_installs_the_handlers_before_any_command(self):
        from unittest import mock
        seen = {}

        def command(a, cfg, aws, say):
            seen["term"] = signal.getsignal(signal.SIGTERM)
            seen["hup"] = signal.getsignal(signal.SIGHUP)
            return 0
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        with mock.patch.object(cli, "cmd_setup", command), mock.patch.object(cli.configmod, "load", lambda: None), \
                mock.patch.object(cli, "make_aws", lambda cfg: None):
            self.assertEqual(cli.main(["setup"]), 0)
        self.assertNotIn(seen["term"], (signal.SIG_DFL, signal.SIG_IGN))
        self.assertNotIn(seen["hup"], (signal.SIG_DFL, signal.SIG_IGN))

    def test_a_signal_during_the_bake_terminates_the_bake_instance(self):
        from oneyaml_bench.cloud import image
        from unittest import mock
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
            aws = FakeAws(lambda spec, n: "hang")
            aws.find_image = lambda digest: None
            with tempfile.TemporaryDirectory() as t:
                def fake_archive(name, repo, dest, paths=(), env_var=None):
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(b"x")
                    return pack.Packed(name, dest, "a" * 40, False, "git", "0" * 64, 1)
                timer = threading.Timer(0.5, os.kill, [os.getpid(), sig])
                timer.start()
                try:
                    with mock.patch.object(image.packmod, "archive_git", fake_archive), mock.patch.object(image, "http_get", lambda u: None):
                        with self.assertRaises(KeyboardInterrupt):
                            image.ensure_image(aws, config.Config("t", "b", "ami"), KIT, Path(t) / "img", lambda m: None, poll=0.05)
                finally:
                    timer.cancel()
            self.assertTrue(aws.launches[0].stop_on_shutdown)
            self.assertEqual([i.state for i in aws.inst.values()], ["terminated"], sig.name)
            self.assertFalse(aws.groups)


class Robustness(RunCase):
    def test_instances_the_listing_does_not_show_yet_are_not_lost(self):
        aws = FakeAws(lambda spec, n: "done-late")
        real = aws.instances
        first_asked = {}

        def lagging(run_id=None):
            out = []
            for i in real(run_id):
                first_asked.setdefault(i.id, time.time())
                if time.time() - first_asked[i.id] > 1.5:
                    out.append(i)
            return out
        aws.instances = lagging
        s = self.make(aws, jobs=job_list(1)).execute()
        self.assertEqual(len(aws.launches), 1, [a for a in s["jobs"][0]["attempts"]])
        self.assertTrue(s["ok"])

    def test_a_retry_that_cannot_finish_before_the_limit_is_not_started(self):
        aws = FakeAws(lambda spec, n: "interrupt")
        jobs = [runner.Job(1, "1/1", "c7i.test", [["bench", "run"]], 5, 600.0)]
        with self.assertRaises(runner.RunFailed) as cm:
            self.make(aws, jobs=jobs, timeout_minutes=2).execute()
        self.assertEqual(len(aws.launches), 1)
        self.assertIn("time limit", str(cm.exception))

    def test_teardown_that_cannot_reach_the_service_still_writes_the_summary_and_names_the_cleanup(self):
        aws = FakeAws(lambda spec, n: "hang")
        real = aws.instances
        state = {"broken": False}

        def instances(run_id=None):
            if state["broken"]:
                raise AwsError("Could not connect to the endpoint URL")
            return real(run_id)
        aws.instances = instances
        r = self.make(aws, jobs=job_list(1))
        r.teardown.wait_seconds = 0.2

        def break_it():
            state["broken"] = True
            r.cancel.set()
        threading.Timer(0.3, break_it).start()
        with self.assertRaises(runner.RunFailed):
            r.execute()
        # With no listing to go by, the instance is terminated by the id the run launched it under.
        self.assertEqual(aws.running(), [])
        self.assertTrue((self.dir / "out" / "cloud-run.json").exists())
        self.assertTrue(any("bin/bench-cloud cleanup --run rtest" in ln for ln in self.lines), self.lines[-3:])

    def test_a_failed_fetch_of_the_result_is_asked_again(self):
        aws = FakeAws()
        r = self.make(aws, jobs=job_list(1))
        calls = []

        def flaky(key):
            calls.append(key)
            if len(calls) < 3:
                if len(calls) == 1:
                    raise AwsError("download failed: HTTP 500")
                return None
            return b"data"
        r._fetch = flaky
        self.assertEqual(r.fetch_result("k"), b"data")
        self.assertEqual(len(calls), 3)
        r._fetch = lambda key: None
        self.assertIsNone(r.fetch_result("k", tries=2))

    def test_sighup_is_shielded_during_the_teardown_like_the_other_signals(self):
        saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            runner.install_signal_handlers()
            runner.shield_interrupts()
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                self.assertEqual(signal.getsignal(sig), signal.SIG_IGN, sig.name)
        finally:
            for s, h in saved.items():
                signal.signal(s, h)

    def test_signing_credentials_must_outlive_the_run(self):
        def fake(args, stdin):
            return json.dumps({"Credentials": {"AccessKeyId": "A", "SecretAccessKey": "S", "SessionToken": "T",
                                               "Expiration": "2026-01-01T01:00:00Z"}})
        a = Aws(config.Config("r", "b", "a"), runner=fake)
        base = dt_epoch("2026-01-01T00:00:00Z")
        with self.assertRaises(AwsError) as cm:
            a.presigner(2 * 3600, now=lambda: base)         # the credentials end after one hour
        self.assertNotIn("S", str(cm.exception).replace("Signing", ""))
        p = a.presigner(1800, now=lambda: base)
        self.assertEqual(p.mode, "session token")
        # Credentials that end a minute short of the deadline are within the allowance for the call's own time.
        p = a.presigner(3600 + 60, now=lambda: base)
        self.assertEqual(p.credentials_expire, base + 3600)

    def test_the_signing_mode_is_recorded(self):
        s = self.make(FakeAws(), jobs=job_list(1)).execute()
        self.assertIn("mode", s["signing"])


def dt_epoch(text):
    import datetime as dt
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


class Cleanup(unittest.TestCase):
    def test_cleanup_removes_tagged_leftovers_and_proves_it(self):
        aws = FakeAws()
        aws.inst["i-9"] = Instance("i-9", "running", "c7i.test", launched=0.0,
                                   tags={RUN_TAG: "old", DEADLINE_TAG: "1000"})
        aws.groups.add("sg-left")
        aws.group_tags["sg-left"] = ("old", 1000.0)
        aws.store["runs/old/inputs.tar"] = b"x"
        lines = []
        left = cleanup.cleanup(aws, None, lines.append, now=lambda: time.time() + 10 ** 6)
        self.assertEqual(cleanup.total(left), 0)
        self.assertTrue(aws.clean())
        self.assertIn("tagged resources left: 0", lines)


class Planning(unittest.TestCase):
    def plan_obj(self, secs, mode="full", lang="go"):
        """The planner's output shape; costs are relative, so the test states seconds and converts."""
        k = plan.SECONDS_PER_COST[mode]
        return {"format": "oneyaml-kit-bench-plan/1", "plan_digest": "sha256:abc", "count": len(secs),
                "shards": [{"index": i, "groups": [f"{lang}/w{i}"], "estimated_cost": s / k, "share": 0.5}
                           for i, s in enumerate(secs, 1)]}

    def test_parse_and_reject(self):
        p = plan.parse_plan(self.plan_obj([100, 200]))
        self.assertEqual([round(s.seconds) for s in p.shards], [100, 200])
        self.assertEqual(p.shards[0].groups, ["go/w1"])
        for bad in ({}, {"plan_digest": "x"}, {"plan_digest": "x", "shards": [{}]},
                    {"plan_digest": "x", "shards": [{"index": 1, "estimated_seconds": 5}]}):
            with self.assertRaises(plan.PlanError):
                plan.parse_plan(bad)
        wrong = self.plan_obj([1, 2])
        wrong["count"] = 3
        with self.assertRaises(plan.PlanError):
            plan.parse_plan(wrong)

    def test_planner_is_asked_with_the_mode_and_the_filters_the_shards_get(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "bin").mkdir()
            (root / "bin" / "bench").write_text(
                "import sys, json\nprint(json.dumps({'plan_digest': 'd', 'shards': [{'index': 1, 'estimated_cost': 1.0, "
                "'groups': sys.argv[1:]}]}))\n")
            filters = cli.filter_args(["go"], ["dump"], None, ["w"])
            got = plan.run_planner(root, 1, "quick", filters)
            self.assertEqual(got.shards[0].groups, ["plan", "--shards", "1", "--mode", "quick", "--lang", "go",
                                                    "--task", "dump", "--workload", "w"])
            job = cli.bench_command("quick", "1/1", filters, 2)
            self.assertEqual(job[:6], ["bench", "run", "--mode", "quick", "--shard", "1/1"])
            self.assertEqual(job[-len(filters):], filters)

    def test_plan_maps_to_one_job_per_shard(self):
        p = plan.parse_plan(self.plan_obj([600, 900, 300]))
        filters = cli.filter_args(["go"], ["dump"], None, None)
        jobs = cli.jobs_from_plan(p, "c7i.test", "full", filters, 2)
        self.assertEqual([j.label for j in jobs], ["1/3", "2/3", "3/3"])
        self.assertEqual({j.instance_type for j in jobs}, {"c7i.test"})
        self.assertEqual({j.role for j in jobs}, {"shard"})
        self.assertEqual(jobs[1].commands[0][:6], ["bench", "run", "--mode", "full", "--shard", "2/3"])
        self.assertEqual(jobs[0].commands[0][-4:], ["--lang", "go", "--task", "dump"])
        self.assertGreater(jobs[1].lifetime_minutes, jobs[2].lifetime_minutes)    # longer shard, longer limit
        self.assertGreater(jobs[1].lifetime_minutes * 60, plan.BOOT_TO_MEASURING_SECONDS + 900)

    def test_quick_mode_has_a_fixed_short_lifetime(self):
        p = plan.parse_plan(self.plan_obj([50000], "quick"), "quick")
        self.assertEqual(cli.jobs_from_plan(p, "t", "quick", [], 2)[0].lifetime_minutes, plan.QUICK_LIFETIME_MINUTES)

    def test_each_further_language_adds_build_time(self):
        two = plan.ShardPlan(1, 100, ["go/a", "rust/a", "csharp/a"])
        one = plan.ShardPlan(1, 100, ["go/a", "go/b"])
        self.assertEqual(plan.instance_seconds(two) - plan.instance_seconds(one), 2 * plan.BUILD_SECONDS_PER_LANGUAGE)

    def test_auto_picks_the_fastest_count_under_the_cap(self):
        def get(n):
            return plan.parse_plan(self.plan_obj([4000.0 / n] * n))
        n, p, table = plan.choose_shards(get, 6, price=1.0, cap=100.0)
        self.assertEqual(n, 6)
        # a low cap rules out the many-instance plans, which pay the start-up time on each instance
        cheap = plan.plan_cost(get(1), 1.0)
        n2, _, _ = plan.choose_shards(get, 6, price=1.0, cap=cheap + 0.03)
        self.assertLess(n2, 6)
        with self.assertRaises(plan.PlanError):
            plan.choose_shards(get, 6, price=1.0, cap=0.01)

    def test_auto_counts_the_conformance_job(self):
        def get(n):
            return plan.parse_plan(self.plan_obj([60.0 / n] * n))
        _, p, _ = plan.choose_shards(get, 4, price=1.0, cap=100.0, conformance=2)
        self.assertGreater(plan.plan_cost(p, 1.0, 2), plan.plan_cost(p, 1.0, 1))
        self.assertGreater(plan.plan_cost(p, 1.0, 1), plan.plan_cost(p, 1.0))
        self.assertGreaterEqual(plan.wall_seconds(p, 2), plan.conformance_instance_seconds(2))

    def test_auto_stops_when_the_harness_cannot_split_further(self):
        def get(n):
            if n > 2:
                raise plan.PlanError("3 shards but only 2 comparison rows to share out")
            return plan.parse_plan(self.plan_obj([600.0 / n] * n))
        n, p, table = plan.choose_shards(get, 8, price=1.0, cap=100.0)
        self.assertEqual(p.count, 2)
        self.assertEqual(len(table), 2)

    def test_cost_guard_refuses_before_launching(self):
        aws = FakeAws()
        aws.spot_price = lambda t: 1.0
        aws.on_demand_price = lambda t: 3.0
        a = type("A", (), dict(out=str(Path(tempfile.mkdtemp())), cost_cap=0.01, kit_tree=True, timeout_minutes=None,
                               spot_retries=2, no_on_demand=False, mode="full"))()
        cfg = config.Config("test", "bucket", "ami-x", cost_cap=25.0)
        with self.assertRaises(runner.RunFailed) as cm:
            cli.prepare_run(a, cfg, aws, lambda m: None, "c7i.test",
                            lambda price: (job_list(2), plan.plan_cost(plan.parse_plan(self.plan_obj([3600, 3600])), price)))
        self.assertIn("above the cap", str(cm.exception))
        self.assertEqual(aws.launches, [])


class Conformance(RunCase):
    def adapters_tree(self, root: Path):
        for name, lang, env in (("oneyaml-go", "go", ["KIT_ONEYAML_GO_SRC"]), ("oneyaml-rust", "rust", []),
                                ("oneyaml-csharp", "csharp", ["KIT_ONEYAML_CSHARP_SRC"]), ("go-yaml-v3", "go", [])):
            d = root / "adapters" / name
            d.mkdir(parents=True)
            (d / "adapter.json").write_text(json.dumps({"name": name, "language": lang, "requires_env": env}))

    def test_own_libraries_of_the_selected_languages_only(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            self.adapters_tree(root)
            self.assertEqual(cli.own_adapters(root, ["go"], {"KIT_ONEYAML_GO_SRC"}), (["oneyaml-go"], []))
            use, skipped = cli.own_adapters(root, ["go", "rust"], set())
            self.assertEqual(use, ["oneyaml-rust"])
            self.assertIn("KIT_ONEYAML_GO_SRC is not set", skipped[0])
            self.assertEqual(cli.own_adapters(root, None, {"KIT_ONEYAML_GO_SRC", "KIT_ONEYAML_CSHARP_SRC"})[0],
                             ["oneyaml-csharp", "oneyaml-go", "oneyaml-rust"])

    def test_conformance_job_runs_the_full_kit_per_library(self):
        j = cli.conformance_job(3, "t", ["oneyaml-go", "oneyaml-rust"])
        self.assertEqual(j.role, "conformance")
        self.assertEqual(j.commands[0], ["kit", "run", "--slow", "--adapter", "oneyaml-go", "--out", "{out}"])
        self.assertEqual(len(j.commands), 2)

    def test_results_are_filed_where_readme_looks(self):
        aws = FakeAws()
        jobs = job_list(1) + [cli.conformance_job(2, "c7i.test", ["oneyaml-go"])]
        r = self.make(aws, jobs=jobs)
        # the fake instance returns a shard result for every job; give the conformance one its file
        orig = aws.launch

        def launch(spec):
            iid = orig(spec)
            script = gzip.decompress(spec.user_data).decode()
            if "kit run" in script:
                url = next(u for u in URL.findall(script) if "result.tar.gz" in u.split("?")[0])
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                    data = b'{"adapter": {"name": "oneyaml-go"}}'
                    info = tarfile.TarInfo("0/results/oneyaml-go.json")
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
                aws.store[url.split("?")[0].split(".com/", 1)[1]] = buf.getvalue()
            return iid
        aws.launch = launch
        calls = []
        r.after_collect = lambda run: calls.append(cli.merge_after_collect(self.dir / "out")(run))
        saved = runner.merge
        runner.merge = lambda *a, **k: calls.append(("merge", a[1], k)) or 0
        try:
            r.execute()
        finally:
            runner.merge = saved
        merge_call = next(c for c in calls if c and c[0] == "merge")
        self.assertEqual([p.parts[-2:] for p in merge_call[1]], [("1", "0")])      # shards only
        self.assertFalse(merge_call[2]["allow_incomplete"])
        self.assertEqual((self.dir / "out" / "results" / "conformance" / "oneyaml-go.json").read_bytes(),
                         b'{"adapter": {"name": "oneyaml-go"}}')

    def test_failed_conformance_still_merges_and_then_fails_the_run(self):
        aws = FakeAws(lambda spec, n: "bad-exit" if n == 2 else "done")
        jobs = job_list(1) + [cli.conformance_job(2, "c7i.test", ["oneyaml-go"])]
        r = self.make(aws, jobs=jobs)
        merged = []
        r.after_collect = lambda run: merged.append(True)
        with self.assertRaises(runner.RunFailed):
            r.execute()
        self.assertEqual(merged, [True])
        self.assertTrue(aws.clean())

    def test_allow_incomplete_merges_the_survivors_and_still_fails(self):
        aws = FakeAws(lambda spec, n: "bad-exit" if n == 2 else "done")
        r = self.make(aws, jobs=job_list(2), allow_incomplete=True)
        seen = []
        r.after_collect = lambda run: seen.append(run.incomplete)
        with self.assertRaises(runner.RunFailed):
            r.execute()
        self.assertEqual(seen, [True])
        self.assertFalse(json.loads((self.dir / "out" / "cloud-run.json").read_text())["ok"])
        self.assertTrue(aws.clean())

    def test_incomplete_is_refused_by_default(self):
        aws = FakeAws(lambda spec, n: "bad-exit" if n == 2 else "done")
        r = self.make(aws, jobs=job_list(2))
        r.after_collect = lambda run: self.fail("merged although a shard failed")
        with self.assertRaises(runner.RunFailed):
            r.execute()


class ShardCells(unittest.TestCase):
    """The check run on the instance after a shard: the result must cover the rows the shard was given."""

    def check(self, obj):
        with tempfile.TemporaryDirectory() as t:
            Path(t, "results.json").write_text(json.dumps(obj))
            Path(t, "check.py").write_text(bootstrap.CHECK_SCRIPT)
            return subprocess.run([sys.executable, str(Path(t, "check.py")), "shard", t], capture_output=True, text=True).returncode

    def shard(self, cells):
        plan = {"groups": {"go/w1": {"tasks": ["dump", "load"], "shard": 1}}}
        return {"run": {"shard": {"index": 1, "count": 1, "plan": plan}, "selection": {"groups": ["go/w1"]}, "environment": {}},
                "harnesses": {"go": {"libraries": [{"id": "lib"}]}}, "cells": cells}

    def cell(self, task, status="ok"):
        return {"language": "go", "library": "lib", "task": task, "workload": "w1", "status": status}

    def test_a_shard_with_every_cell_passes(self):
        self.assertEqual(self.check(self.shard([self.cell("dump"), self.cell("load", "differs")])), 0)

    def test_zero_cells_fail(self):
        self.assertEqual(self.check(self.shard([])), 25)

    def test_zero_cells_fail_even_without_a_plan(self):
        self.assertEqual(self.check({"run": {"shard": {"index": 1, "count": 2}, "environment": {}}, "cells": []}), 25)

    def test_a_missing_cell_of_a_held_row_fails(self):
        self.assertEqual(self.check(self.shard([self.cell("dump")])), 25)

    def test_a_shard_that_says_it_fell_short_fails(self):
        obj = self.shard([self.cell("dump"), self.cell("load")])
        obj["run"]["publishability"] = {"reasons": ["the run did not finish every cell it set out to measure"]}
        self.assertEqual(self.check(obj), 25)

    def test_an_error_cell_fails_the_whole_shard(self):
        self.assertEqual(self.check(self.shard([self.cell("dump"), self.cell("load", "error")])), 24)


class ShardCheck(unittest.TestCase):
    """What the instance accepts as a shard that measured correctly, run on the script it really uses."""

    def check(self, kind, files):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "0"
            out.mkdir()
            for name, data in files.items():
                (out / name).parent.mkdir(parents=True, exist_ok=True)
                (out / name).write_text(data if isinstance(data, str) else json.dumps(data))
            script = Path(t) / "check.py"
            script.write_text(bootstrap.CHECK_SCRIPT)
            label = Path(t) / "kit-label"
            r = subprocess.run([sys.executable, str(script), kind, str(out)], capture_output=True, text=True)
            return r.returncode, r.stdout

    def shard(self, status="ok", **env):
        return {"run": {"shard": {"index": 1}, "environment": dict({"instance_type": "t"}, **env)},
                "cells": [{"library": "l", "task": "t", "workload": "w", "status": status}]}

    def test_non_publishable_shard_with_deviations_is_a_success(self):
        for status in ("ok", "differs", "rejects", "unsupported"):
            self.assertEqual(self.check("shard", {"results.json": self.shard(status)})[0], 0, status)

    def test_a_crashed_or_stalled_cell_is_a_failure(self):
        code, out = self.check("shard", {"results.json": self.shard("error")})
        self.assertEqual(code, 24)
        self.assertIn("crashed or stalled", out)

    def test_missing_unreadable_or_unsharded_results_are_failures(self):
        self.assertEqual(self.check("shard", {})[0], 23)
        self.assertEqual(self.check("shard", {"results.json": "{not json"})[0], 23)
        self.assertEqual(self.check("shard", {"results.json": {"run": {}, "cells": []}})[0], 23)

    def test_conformance_needs_its_result_file(self):
        self.assertEqual(self.check("conformance", {})[0], 22)
        self.assertEqual(self.check("conformance", {"results/oneyaml-go.json": {}})[0], 0)


class Scripts(unittest.TestCase):
    def script(self, **kw):
        long = "https://bucket.s3.us-east-2.amazonaws.com/runs/r/x?" + "X-Amz-Signature=" + "a" * 64 + "&X-Amz-Security-Token=" + "t" * 900
        args = dict(run_id="r", shard="1/2", attempt=1, commands=[["bench", "run", "--mode", "full", "--shard", "1/2", "--out", "{out}"],
                              ["kit", "run", "--slow", "--out", "{out}"]],
                    inputs_url=long, log_url=long, status_url=long, result_url=long, lifetime_minutes=33,
                    library_vars={"KIT_ONEYAML_GO_SRC": "oneyaml-go"})
        args.update(kw)
        return bootstrap.shard_script(bootstrap.ShardScript(**args))

    def test_lifetime_limit_comes_before_any_other_command(self):
        lines = [l for l in self.script().splitlines() if l and not l.startswith("#")]
        self.assertEqual(lines[0], "set -u")
        self.assertTrue(lines[1].startswith("shutdown -h +33 "))

    def test_every_exit_powers_off_and_no_credential_is_placed(self):
        s = self.script()
        self.assertIn("trap 'finish' EXIT", s)
        self.assertIn("shutdown -h now", s.split("finish() {")[1].split("\n}\n")[0])
        self.assertNotRegex(s, r"AKIA|aws_secret|AWS_SECRET|AWS_ACCESS")
        self.assertIn("KIT_INSTANCE_TYPE", s)

    def test_syntax_is_valid_shell(self):
        for s in (self.script(), bootstrap.bake_script("u", "u", "u", 40, "r")):
            r = subprocess.run(["bash", "-n"], input=s, text=True, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_user_data_fits_the_limit_with_realistic_urls(self):
        self.assertLess(len(bootstrap.pack_user_data(self.script())), 16 * 1024)
        self.assertEqual(gzip.decompress(bootstrap.pack_user_data("echo hi\n")), b"echo hi\n")

    def test_bake_stops_instead_of_terminating_and_pins_versions(self):
        s = bootstrap.bake_script("u", "u", "u", 40, "r")
        self.assertIn(toolchains.PINS["rust"], s)
        self.assertIn(toolchains.go_url(), s)
        self.assertIn(toolchains.dotnet_url(), s)

    def test_launch_hardening(self):
        from oneyaml_bench.cloud.aws import Aws, Launch
        sent = {}

        def fake(args, stdin):
            sent["args"] = args
            ref = args[args.index("--cli-input-json") + 1]
            sent["ref"] = ref
            path = Path(ref[len("file://"):])
            sent["mode"] = path.stat().st_mode & 0o777
            sent["body"] = path.read_text()
            sent["path"] = path
            return json.dumps({"Instances": [{"InstanceId": "i-1"}]})
        with tempfile.TemporaryDirectory() as t:
            Aws(config.Config("r", "b", "a"), runner=fake, tmpdir=t).launch(
                Launch("ami", "t", "sg", b"secret-user-data", "run", "n", True, 20, deadline=1234.0, zone="z-a"))
            # The request holds presigned URLs: it goes in by a private file, never on the command line.
            self.assertTrue(sent["ref"].startswith("file://" + t))
            self.assertEqual(sent["mode"], 0o600)
            self.assertFalse(sent["path"].exists(), "the request file is removed after the call")
        self.assertNotIn("c2VjcmV0", " ".join(sent["args"]))
        req = json.loads(sent["body"])
        self.assertEqual(req["Placement"], {"AvailabilityZone": "z-a"})
        self.assertTrue(all({"Key": DEADLINE_TAG, "Value": "1234"} in t["Tags"] for t in req["TagSpecifications"]))
        self.assertEqual(req["MetadataOptions"]["HttpTokens"], "required")
        self.assertEqual(req["InstanceInitiatedShutdownBehavior"], "terminate")
        for forbidden in ("KeyName", "IamInstanceProfile"):
            self.assertNotIn(forbidden, req)
        self.assertEqual(req["InstanceMarketOptions"]["MarketType"], "spot")
        self.assertTrue(all({"Key": "oneyaml-kit-run", "Value": "run"} in t["Tags"] for t in req["TagSpecifications"]))


class Signing(unittest.TestCase):
    def test_published_example(self):
        import datetime as dt
        u = sigv4.presign("GET", "examplebucket.s3.amazonaws.com", "test.txt", "us-east-1", "AKIAIOSFODNN7EXAMPLE",
                          "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", None, 86400, dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc))
        self.assertTrue(u.endswith("aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"))

    def test_method_is_part_of_the_signature(self):
        a = sigv4.presign("GET", "h", "k", "r", "A", "s", None, 60)
        b = sigv4.presign("PUT", "h", "k", "r", "A", "s", None, 60)
        self.assertNotEqual(a.split("Signature=")[1], b.split("Signature=")[1])


class PackAndImage(unittest.TestCase):
    def repo(self, d: Path):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        for cmd in (["init", "-q", "."], ["add", "-A"], ["commit", "-q", "-m", "m"]):
            if cmd[0] == "add":
                (d / "a.txt").write_text("one\n")
            subprocess.run(["git", "-C", str(d)] + cmd, check=True, env=env, capture_output=True)

    def test_archive_is_head_and_records_dirty(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "src"
            d.mkdir()
            self.repo(d)
            (d / "a.txt").write_text("two\n")
            p = pack.archive_git("lib", d, Path(t) / "lib.tar.gz")
            self.assertTrue(p.dirty)
            self.assertEqual(len(p.revision), 40)
            with tarfile.open(p.path) as tf:
                self.assertEqual(tf.extractfile("a.txt").read(), b"one\n")
            self.assertEqual(pack.describe([p])[0]["revision"], p.revision)
            self.assertNotIn(t, json.dumps(pack.describe([p])))

    def test_tree_pack_follows_git_and_is_marked_modified(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "kit"
            (d / "bench").mkdir(parents=True)
            (d / "bench" / "kept.py").write_text("a = 1\n")
            (d / "bench" / "gone.py").write_text("b = 1\n")
            (d / ".gitignore").write_text("*.cache\n")
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
            for cmd in (["init", "-q", "."], ["add", "-A"], ["commit", "-q", "-m", "m"]):
                subprocess.run(["git", "-C", str(d)] + cmd, check=True, env=env, capture_output=True)
            (d / "bench" / "kept.py").write_text("a = 2\n")            # edited
            (d / "bench" / "new.py").write_text("c = 1\n")             # not added yet
            (d / "bench" / "junk.cache").write_text("x")                # ignored by the repository
            (d / "bench" / "gone.py").unlink()                          # deleted but still tracked
            packs = pack.pack_all(d, Path(t) / "packs", kit_tree=True, env={})
            with tarfile.open(packs[0].path) as tf:
                names = sorted(tf.getnames())
                self.assertEqual(tf.extractfile("bench/kept.py").read(), b"a = 2\n")
            self.assertEqual(names, ["bench/kept.py", "bench/new.py"])
            bundle = pack.bundle_inputs(packs, Path(t) / "in.tar")
            with tarfile.open(bundle) as tf:
                rev = tf.extractfile("rev-kit").read().decode().strip()
                label = tf.extractfile("kit-label").read().decode().strip()
            self.assertEqual(rev, packs[0].revision + "+modified")
            self.assertTrue(label.endswith("+modified"))

    def test_git_pack_records_the_plain_revision(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "src"
            d.mkdir()
            self.repo(d)
            p = pack.archive_git("kit", d, Path(t) / "k.tar.gz")
            with tarfile.open(pack.bundle_inputs([p], Path(t) / "in.tar")) as tf:
                self.assertEqual(tf.extractfile("rev-kit").read().decode().strip(), p.revision)

    def test_image_digest_follows_lock_files_and_pins(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            for rel in toolchains.LOCK_FILES:
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("v1")
            d0 = toolchains.digest(root, "ami-1", "script")
            self.assertEqual(d0, toolchains.digest(root, "ami-1", "script"))
            (root / "bench/go/go.sum").write_text("v2")
            self.assertNotEqual(d0, toolchains.digest(root, "ami-1", "script"))
            self.assertNotEqual(d0, toolchains.digest(root, "ami-2", "script"))
            self.assertNotEqual(d0, toolchains.digest(root, "ami-1", "script2"))

    def test_merge_runs_the_harness_command(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "bin").mkdir()
            (root / "bin" / "bench").write_text("import sys, json\nprint(json.dumps(sys.argv[1:]))\n")
            lines = []
            rc = runner.merge(root, [Path("a/0"), Path("b/0")], Path("m"), lines.append)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(lines[-1]), ["merge", "--out", "m", "a/0", "b/0"])


class Config(unittest.TestCase):
    def test_parse_and_required(self):
        v = config.parse("# c\nKIT_CLOUD_REGION=r\nKIT_CLOUD_BUCKET='b'\nKIT_CLOUD_BASE_AMI=a\n")
        self.assertEqual(config.from_values(v).bucket, "b")
        with self.assertRaises(config.ConfigError):
            config.from_values({"KIT_CLOUD_REGION": "r"})


if __name__ == "__main__":
    unittest.main()
