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
from oneyaml_bench.cloud.aws import Instance, LaunchError, Presigner  # noqa: E402

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

    def create_security_group(self, run_id):
        gid = f"sg-{run_id}"
        self.groups.add(gid)
        return gid

    def delete_security_group(self, gid, attempts=1):
        self.groups.discard(gid)
        return True

    def security_groups(self, run_id=None):
        return sorted(self.groups)

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
        self.inst[iid] = Instance(iid, "running", spec.type, "spot" if spec.spot else "on-demand", "test-a", time.time(), tags={})
        script = gzip.decompress(spec.user_data).decode()
        urls = {u.split("?")[0].rsplit("/", 1)[1]: u.split("?")[0].split(".com/", 1)[1] for u in URL.findall(script)}
        if action in ("done", "bad-exit", "done-slow"):
            now = time.time()
            self.store[urls["log.txt"]] = b"== run\nmeasuring (quick)\n"
            self.store[urls["result.tar.gz"]] = fake_result(spec.type)
            self.store[urls["status.json"]] = json.dumps({
                "done": True, "exit": 3 if action == "bad-exit" else 0, "governor": "performance",
                "marks": {"boot": now - 30, "script_start": now - 25, "setup_done": now - 20, "inputs_ready": now - 15,
                          "cache_warm": now - 14, "bench_start": now - 13, "measuring": now - 10, "bench_done": now - 1,
                          "finished": now},
                "instance": {"KIT_INSTANCE_TYPE": spec.type, "KIT_INSTANCE_AZ": "test-a", "KIT_INSTANCE_LIFECYCLE": "spot"}}).encode()
            self.inst[iid].state = "shutting-down" if action == "done-slow" else "terminated"
        elif action == "interrupt":
            self.inst[iid].state = "terminated"
            self.inst[iid].reason = "Server.SpotInstanceTermination"
        return iid

    def instances(self, run_id=None):
        return [Instance(i.id, i.state, i.type, i.lifecycle, i.az, i.launched, reason=i.reason) for i in self.inst.values()]

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
    return [runner.Job(i, f"{i}/{n}", itype, [["run", "--shard", f"{i}/{n}", "--out", "{out}"]], minutes, 60.0) for i in range(1, n + 1)]


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

    def make(self, aws, jobs=None, **opts):
        o = runner.Options(out=self.dir / "out", poll_seconds=0.02, launch_backoff_seconds=0, **opts)
        r = runner.Run(aws, config.Config("test", "bucket", "ami-x"), o, jobs or job_list(), "ami-image", self.packs, 0.5, 1.5,
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


class Cleanup(unittest.TestCase):
    def test_cleanup_removes_tagged_leftovers_and_proves_it(self):
        aws = FakeAws()
        aws.inst["i-9"] = Instance("i-9", "running", "c7i.test")
        aws.groups.add("sg-left")
        aws.store["runs/old/inputs.tar"] = b"x"
        lines = []
        left = cleanup.cleanup(aws, None, lines.append, now=lambda: time.time() + 10 ** 6)
        self.assertEqual(cleanup.total(left), 0)
        self.assertTrue(aws.clean())
        self.assertIn("tagged resources left: 0", lines)


class Planning(unittest.TestCase):
    def plan_obj(self, secs):
        return {"plan_digest": "abc", "shards": [{"shard": i, "groups": ["g%d" % i], "estimated_seconds": s}
                                                 for i, s in enumerate(secs, 1)]}

    def test_parse_and_reject(self):
        p = plan.parse_plan(self.plan_obj([100, 200]), 2)
        self.assertEqual([s.seconds for s in p.shards], [100, 200])
        for bad in ({}, {"plan_digest": "x"}, {"plan_digest": "x", "shards": [{}]}):
            with self.assertRaises(plan.PlanError):
                plan.parse_plan(bad)
        with self.assertRaises(plan.PlanError):
            plan.parse_plan(self.plan_obj([1]), 2)

    def test_plan_maps_to_one_job_per_shard(self):
        p = plan.parse_plan(self.plan_obj([600, 900, 300]))
        jobs = cli.jobs_from_plan(p, "c7i.test", "full", ["go"], 2, ["--task", "dump"])
        self.assertEqual([j.label for j in jobs], ["1/3", "2/3", "3/3"])
        self.assertEqual({j.instance_type for j in jobs}, {"c7i.test"})
        self.assertEqual(jobs[1].commands[0][:5], ["run", "--mode", "full", "--shard", "2/3"])
        self.assertIn("go", jobs[0].commands[0])
        self.assertEqual(jobs[0].commands[0][-2:], ["--task", "dump"])
        self.assertGreater(jobs[1].lifetime_minutes, jobs[2].lifetime_minutes)    # longer shard, longer limit
        self.assertGreater(jobs[1].lifetime_minutes * 60, plan.BOOT_TO_MEASURING_SECONDS + 900)

    def test_quick_mode_has_a_fixed_short_lifetime(self):
        p = plan.parse_plan(self.plan_obj([50000]))
        self.assertEqual(cli.jobs_from_plan(p, "t", "quick", None, 2, [])[0].lifetime_minutes, plan.QUICK_LIFETIME_MINUTES)

    def test_auto_picks_the_fastest_count_under_the_cap(self):
        totals = [4000.0]

        def get(n):
            return plan.parse_plan(self.plan_obj([totals[0] / n] * n))
        n, p, table = plan.choose_shards(get, 6, price=1.0, cap=100.0)
        self.assertEqual(n, 6)
        # a low cap rules out the many-instance plans, which pay the start-up time on each instance
        cheap = plan.plan_cost(get(1), 1.0)
        n2, _, _ = plan.choose_shards(get, 6, price=1.0, cap=cheap + 0.3)
        self.assertLess(n2, 6)
        with self.assertRaises(plan.PlanError):
            plan.choose_shards(get, 6, price=1.0, cap=0.01)

    def test_auto_stops_when_the_harness_cannot_split_further(self):
        def get(n):
            return plan.parse_plan(self.plan_obj([600.0 / min(n, 2)] * min(n, 2)))
        n, p, table = plan.choose_shards(get, 8, price=1.0, cap=100.0)
        self.assertEqual(p.count, 2)
        self.assertLessEqual(len(table), 3)

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


class Scripts(unittest.TestCase):
    def script(self, **kw):
        long = "https://bucket.s3.us-east-2.amazonaws.com/runs/r/x?" + "X-Amz-Signature=" + "a" * 64 + "&X-Amz-Security-Token=" + "t" * 900
        args = dict(run_id="r", shard="1/2", attempt=1, commands=[["run", "--mode", "full", "--shard", "1/2", "--out", "{out}"]],
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
            return json.dumps({"Instances": [{"InstanceId": "i-1"}]})
        Aws(config.Config("r", "b", "a"), runner=fake).launch(Launch("ami", "t", "sg", b"x", "run", "n", True, 20))
        req = json.loads(sent["args"][sent["args"].index("--cli-input-json") + 1])
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
