"""Runs jobs on short-lived instances and guarantees that nothing is left running.

One job is one instance: it gets the packed inputs and a list of `bin/bench` commands, runs
them, and returns its output directory. The launcher starts every job in parallel, follows the
logs the instances stream to the bucket, retries a job whose instance was lost (a few times on
spot, then once on demand, always the same instance type), and finally terminates everything
tagged with the run's id. The teardown runs on every way out: success, failure, a timeout,
Ctrl-C, SIGTERM and SIGHUP.

Money is bounded by one rule: before any instance is started, spot retries included, the cost already
committed by every live and finished attempt of every job plus the full projected cost of the new one must
fit under the cost cap.
"""

from __future__ import annotations

import io
import json
import signal
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import bootstrap, pack as packmod, plan as planmod
from .aws import AwsError, Launch, LaunchError, http_get

# Launch errors that mean "no capacity or price right now": worth another try.
RETRYABLE_LAUNCH = {"InsufficientInstanceCapacity", "SpotMaxPriceTooLow", "MaxSpotInstanceCountExceeded",
                    "VcpuLimitExceeded", "InternalError", "Unsupported"}
ENDED_STATES = {"shutting-down", "terminated", "stopping", "stopped"}


class RunFailed(Exception):
    pass


@dataclass
class Job:
    index: int
    label: str
    instance_type: str
    commands: list[list[str]]
    lifetime_minutes: int
    est_seconds: float = 0.0
    role: str = "shard"              # "shard" (timed; merged) or "conformance" (untimed; kept as it is)


@dataclass
class Attempt:
    number: int
    lifecycle: str                    # spot or on-demand
    instance_id: str | None = None
    instance_type: str = ""
    launched: float = 0.0
    ended: float | None = None
    outcome: str = "pending"          # done, lost, launch-failed, failed, cancelled
    detail: str = ""
    status: dict = field(default_factory=dict)
    reserved: bool = False            # counted against the cost cap
    rate: float = 0.0                 # dollars per hour at reservation
    est_seconds: float = 0.0          # the projected life of the instance
    zone: str = ""


@dataclass
class JobResult:
    job: Job
    attempts: list[Attempt] = field(default_factory=list)
    out_dir: Path | None = None
    partial_dir: Path | None = None   # the output of a job that failed; never merged
    ok: bool = False
    error: str = ""


@dataclass
class Options:
    out: Path
    mode: str = "full"
    spot_retries: int = 2
    on_demand_fallback: bool = True
    cost_cap: float = 25.0
    poll_seconds: float = 5.0
    timeout_minutes: int | None = None
    volume_gb: int = 40
    launch_backoff_seconds: float = 10.0
    # The instance list can lag behind a launch; an instance that has not shown up yet is not a lost one.
    missing_grace_seconds: float = 90.0
    allow_incomplete: bool = False   # merge the shards that succeeded; the run still counts as failed


class Teardown:
    """Terminates and deletes everything a run created. Safe to call more than once.

    Bare metal instances take minutes to finish terminating. An instance that has started shutting down
    no longer runs and is no longer billed, so the wait for the final state is bounded; the security group
    that such an instance still holds is deleted by the next cleanup, which waits as long as it takes."""

    def __init__(self, aws, run_id: str | None, say=print, poll: float = 3.0, wait_seconds: float = 30.0):
        """With run_id None it covers every run that left tagged resources, for the cleanup command."""
        self.aws, self.run_id, self.say, self.poll, self.wait_seconds = aws, run_id, say, poll, wait_seconds
        self.security_groups: list[str] = []
        self.known_instances: set[str] = set()      # what this run launched, for when the listing is unavailable
        self.done = False
        self.pending: list[str] = []

    def live(self):
        return [i for i in self.aws.instances(self.run_id) if i.state != "terminated"]

    def request(self) -> list[str]:
        """Asks for everything that is not already shutting down to be terminated. Returns problems."""
        problems: list[str] = []
        try:
            active = [i.id for i in self.live() if i.state != "shutting-down"]
        except AwsError as e:
            # The listing failed: terminate by the ids this run knows, which does not need it.
            problems.append(f"list instances: {e}")
            active = sorted(self.known_instances)
        try:
            if active:
                self.aws.terminate(active)
            self.aws.cancel_spot_requests(self.run_id)
        except AwsError as e:
            problems.append(f"terminate: {e}")
        return problems

    @staticmethod
    def _step(problems: list[str], what: str, fn, default=None):
        """One teardown step. A failure is recorded and the steps after it still run."""
        try:
            return fn()
        except AwsError as e:
            problems.append(f"{what}: {e}")
            return default

    def run(self, quiet: bool = False, wait: float | None = None) -> list[str]:
        """Returns the problems that remain: instances that still run, or resources that could not be deleted.
        Instances that are shutting down are listed in `pending`, not as problems."""
        problems: list[str] = []
        self.pending = []
        end = time.monotonic() + (self.wait_seconds if wait is None else wait)
        live = None
        while True:
            problems = self.request()
            live = self._step(problems, "list instances", self.live)
            if live == [] or time.monotonic() > end:
                break
            time.sleep(self.poll)
        running = [i for i in live or [] if i.state != "shutting-down"]
        if running:
            problems.append("instances still running: " + ", ".join(i.id for i in running))
        if live:
            self.pending = [f"{i.id} is shutting down" for i in live if i.state == "shutting-down"]
        groups = {*self.security_groups, *(self._step(problems, "list security groups",
                                                      lambda: self.aws.security_groups(self.run_id), []) or [])}
        if live is None:
            problems.append("the instances could not be listed, so nothing was deleted that they might hold")
        elif live:
            if groups:
                self.pending.append("security group(s) wait for those instances; the cleanup command removes them")
        else:
            for gid in groups:
                if not self._step(problems, f"delete security group {gid}", lambda: self.aws.delete_security_group(gid)):
                    problems.append(f"security group {gid} could not be deleted")
        for lid in self._step(problems, "list launch templates", lambda: self.aws.launch_templates(self.run_id), []) or []:
            self._step(problems, f"delete launch template {lid}", lambda: self.aws.delete_launch_template(lid))
        for vid in self._step(problems, "list volumes", lambda: self.aws.volumes(self.run_id), []) or []:
            self._step(problems, f"delete volume {vid}", lambda: self.aws.delete_volume(vid))
        if self.run_id:
            self._step(problems, "delete stored objects", lambda: self.aws.delete_prefix(f"runs/{self.run_id}/"))
        if self.pending:
            # What waits for a terminating instance is the cleanup command's to remove. Marking it as due now
            # lets a plain cleanup (which spares runs inside their deadline) take it as soon as the instance is gone.
            held = [i.id for i in live or []] + sorted(groups)
            self._step(problems, "mark leftovers as due", lambda: self.aws.expire(held))
        self.done = not problems and not self.pending
        if not quiet:
            self.say("teardown: " + ("nothing left" if self.done else "; ".join(problems + self.pending) or "done"))
        return problems


class Run:
    def __init__(self, aws, cfg, opts: Options, jobs: list[Job], image_id: str, packs: list[packmod.Packed], price: float,
                 on_demand_price: float | None, say=print, fetch=None, run_id: str | None = None, clock=time.time,
                 after_collect=None):
        self.aws, self.cfg, self.opts, self.jobs, self.image_id, self.packs = aws, cfg, opts, jobs, image_id, packs
        self.price, self.on_demand_price = price, on_demand_price
        self.after_collect = after_collect
        self.incomplete = False
        self.deferred: list[str] = []
        self.extra: dict = {}
        self.prices: dict[str, float] = {}      # per type, when a run mixes types
        self.say = say
        self.run_id = run_id or "r" + time.strftime("%m%d%H%M%S") + uuid.uuid4().hex[:4]
        self.clock = clock
        self.cancel = threading.Event()
        self.teardown = Teardown(aws, self.run_id, say, poll=min(3.0, opts.poll_seconds))
        self.results: dict[int, JobResult] = {}
        self.lock = threading.Lock()
        self.commit_lock = threading.Lock()     # makes "check the cap, then reserve" one step
        self.limit_at: float | None = None      # when the whole run must be over
        self.signing: dict = {}
        self.presigner = None
        self._fetch = fetch
        self.sg: str | None = None
        self.timeline: dict[str, float] = {}
        self._snap_time = 0.0
        self._snap: dict = {}

    # -- helpers ----------------------------------------------------------------------------

    def fetch(self, key: str) -> bytes | None:
        if self._fetch:
            return self._fetch(key)
        return http_get(self.presigner.url("GET", key))

    def key(self, *parts: str) -> str:
        return "/".join(("runs", self.run_id) + parts)

    def instance_snapshot(self) -> dict:
        """One describe call shared by all job threads per poll interval."""
        with self.lock:
            if self.clock() - self._snap_time >= self.opts.poll_seconds / 2:
                self._snap = {i.id: i for i in self.aws.instances(self.run_id)}
                self._snap_time = self.clock()
            return dict(self._snap)

    def hourly(self, lifecycle: str, itype: str) -> float | None:
        """Dollars per hour, volume included; None for on-demand when its price is not known."""
        if lifecycle == "on-demand":
            return self.on_demand_price
        return self.prices.get(itype, self.price)

    def projected_cost(self, extra_seconds: float = 0.0, on_demand: bool = False) -> float:
        price = self.hourly("on-demand" if on_demand else "spot", "")
        return planmod.shard_cost(price or 0.0, extra_seconds)

    def committed(self, a: Attempt) -> float:
        """What an attempt counts for against the cap: what it cost when it is over, and while it lives the larger
        of what it has cost so far and what its whole projected life will cost."""
        if not a.reserved or (a.outcome == "launch-failed" and not a.instance_id):
            return 0.0
        if a.ended is not None:
            return self.cost_of(a)
        return max(self.cost_of(a), planmod.shard_cost(a.rate, a.est_seconds))

    def committed_total(self) -> float:
        with self.lock:
            attempts = [a for r in self.results.values() for a in list(r.attempts)]
        return sum(self.committed(a) for a in attempts)

    def reserve(self, job: Job, att: Attempt) -> str:
        """Checks the attempt against the cap together with everything already committed, and reserves it.
        Returns the reason it was refused, or an empty string. Every launch goes through here."""
        with self.commit_lock:
            rate = self.hourly(att.lifecycle, job.instance_type)
            if rate is None:
                return f"the {att.lifecycle} price is not known, so its cost cannot be bounded"
            need = planmod.shard_cost(rate, job.est_seconds)
            spent = self.committed_total()
            if spent + need > self.opts.cost_cap:
                return (f"a {att.lifecycle} instance would take the committed cost from ${spent:.2f} to "
                        f"${spent + need:.2f}, above the cap of ${self.opts.cost_cap:.2f}")
            att.rate, att.est_seconds, att.reserved = rate, job.est_seconds, True
            att.launched = self.clock()
            return ""

    # -- one job ----------------------------------------------------------------------------

    def run_job(self, job: Job) -> None:
        res = JobResult(job)
        with self.lock:
            self.results[job.index] = res
        fallback = self.opts.on_demand_fallback and self.on_demand_price is not None
        schedule = ["spot"] * (1 + self.opts.spot_retries) + (["on-demand"] if fallback else [])
        for n, lifecycle in enumerate(schedule, 1):
            if self.cancel.is_set():
                res.error = res.error or "cancelled"
                return
            if n > 1 and self.limit_at is not None and self.clock() + job.est_seconds > self.limit_at:
                res.error = res.error or "no attempt left can finish before the run's time limit"
                self.say(f"[{job.label}] {res.error}")
                return
            att = Attempt(n, lifecycle)
            res.attempts.append(att)
            refused = self.reserve(job, att)
            if refused:
                att.outcome, att.detail = "launch-failed", refused
                res.error = refused
                self.say(f"[{job.label}] not launched: {refused}")
                return
            try:
                self.launch(job, att)
            except LaunchError as e:
                att.outcome, att.detail = "launch-failed", str(e)
                self.say(f"[{job.label}] {lifecycle} launch failed: {e.code}")
                if e.code not in RETRYABLE_LAUNCH:
                    res.error = f"launch failed: {e}"
                    return
                self.cancel.wait(self.opts.launch_backoff_seconds)
                continue
            except AwsError as e:
                att.outcome, att.detail = "launch-failed", str(e)
                res.error = f"launch failed: {e}"
                return
            self.wait_for(job, att, res)
            if att.outcome == "done":
                self.collect(job, att, res)
                return
            if att.outcome == "failed":
                res.error = att.detail
                self.collect(job, att, res, ok=False)      # kept for diagnosis; never merged
                return
            if att.outcome == "cancelled":
                res.error = "cancelled"
                return
            self.say(f"[{job.label}] attempt {n} ({lifecycle}) lost: {att.detail}; "
                     + ("retrying" if n < len(schedule) else "no attempts left"))
        res.error = res.error or "every attempt was lost"

    def launch(self, job: Job, att: Attempt) -> None:
        prefix = f"a{att.number}"
        ttl = job.lifetime_minutes
        script = bootstrap.shard_script(bootstrap.ShardScript(
            run_id=self.run_id, shard=job.label, attempt=att.number, commands=job.commands,
            inputs_url=self.presigner.url("GET", self.key("inputs.tar")),
            log_url=self.presigner.url("PUT", self.key(f"job{job.index}-{prefix}", "log.txt")),
            status_url=self.presigner.url("PUT", self.key(f"job{job.index}-{prefix}", "status.json")),
            result_url=self.presigner.url("PUT", self.key(f"job{job.index}-{prefix}", "result.tar.gz")),
            lifetime_minutes=ttl,
            library_vars={p.env_var: p.name for p in self.packs if p.env_var}))
        # Spot attempts walk through the configured zones one by one: capacity differs from zone to zone.
        zones = list(getattr(self.cfg, "zones", ()) or ())
        att.zone = zones[(att.number - 1) % len(zones)] if zones and att.lifecycle == "spot" else ""
        spec = Launch(image=self.image_id, type=job.instance_type, security_group=self.sg,
                      user_data=bootstrap.pack_user_data(script), run_id=self.run_id,
                      name=f"oneyaml-kit-{self.run_id}-{job.index}", spot=att.lifecycle == "spot",
                      volume_gb=self.opts.volume_gb, zone=att.zone,
                      deadline=self.clock() + (job.lifetime_minutes + 10) * 60)
        att.instance_type = job.instance_type
        att.instance_id = self.aws.launch(spec)
        self.teardown.known_instances.add(att.instance_id)
        att.outcome = "running"
        self.say(f"[{job.label}] launched {att.instance_id} ({att.lifecycle}, attempt {att.number})")

    def wait_for(self, job: Job, att: Attempt, res: JobResult) -> None:
        base = self.key(f"job{job.index}-a{att.number}")
        deadline = att.launched + (job.lifetime_minutes + 5) * 60
        shown = 0
        gone_since: float | None = None
        while True:
            if self.cancel.is_set():
                att.outcome = "cancelled"
                return
            raw = self.fetch(base + "/status.json")
            log = self.fetch(base + "/log.txt")
            if log:
                text = log.decode("utf-8", "replace")
                for line in text[shown:].splitlines():
                    self.say(f"[{job.label}] {line}")
                shown = len(text)
            if raw:
                att.status = json.loads(raw)
                att.ended = att.status.get("marks", {}).get("finished", self.clock())
                if att.status.get("exit") == 0:
                    att.outcome = "done"
                else:
                    att.outcome, att.detail = "failed", f"the run exited with status {att.status.get('exit')}"
                return
            inst = self.instance_snapshot().get(att.instance_id)
            # A listing that lags behind the launch does not show the instance yet; that is not a loss.
            missing_too_soon = inst is None and self.clock() - att.launched < self.opts.missing_grace_seconds
            if not missing_too_soon and (inst is None or inst.state in ENDED_STATES):
                # The status is uploaded just before power-off: look once more after a pause.
                if gone_since is None:
                    gone_since = self.clock()
                elif self.clock() - gone_since > max(2 * self.opts.poll_seconds, 1):
                    att.ended = self.clock()
                    att.outcome = "lost"
                    att.detail = f"instance ended ({inst.state if inst else 'missing'}"\
                                 f"{', ' + inst.reason if inst and inst.reason else ''}) without a result"
                    return
            if self.clock() > deadline:
                att.outcome, att.detail = "lost", "no result before the deadline"
                att.ended = self.clock()
                try:
                    self.aws.terminate([att.instance_id])
                except AwsError:
                    pass
                return
            self.cancel.wait(self.opts.poll_seconds)

    def collect(self, job: Job, att: Attempt, res: JobResult, ok: bool = True) -> None:
        data = self.fetch(self.key(f"job{job.index}-a{att.number}", "result.tar.gz"))
        if not data:
            if ok:
                att.outcome, att.detail = "failed", "the instance reported success but uploaded no result"
                res.error = att.detail
            return
        dest = self.opts.out / "jobs" / str(job.index)
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            tf.extractall(dest, filter="data")
        if ok:
            res.out_dir, res.ok = dest, True
        else:
            res.partial_dir = dest
            self.say(f"[{job.label}] the results of the failed job were kept for inspection in jobs/{job.index}")

    # -- the whole run ----------------------------------------------------------------------

    def cost_of(self, a: Attempt) -> float:
        if not a.instance_id or not a.launched:
            return 0.0
        end = a.ended or self.clock()
        price = a.rate or self.hourly(a.lifecycle, a.instance_type or "") or 0.0
        return price * max(planmod.MIN_BILLED_SECONDS, end - a.launched) / 3600.0

    def execute(self) -> dict:
        """Runs every job and returns the summary. Raises RunFailed after the teardown when a job failed."""
        t0 = self.clock()
        error: BaseException | None = None
        threads: list[threading.Thread] = []
        try:
            self.say(f"run {self.run_id}: {len(self.jobs)} job(s) of {self.types()}, image {self.image_id}")
            longest = max(j.lifetime_minutes for j in self.jobs)
            self.sg = self.aws.create_security_group(self.run_id, self.clock() + (longest + 30) * 60)
            self.teardown.security_groups.append(self.sg)
            self.presigner = self.aws.presigner((longest + 30) * 60)
            self.signing = {"mode": getattr(self.presigner, "mode", "unknown"),
                            "credentials_expire": getattr(self.presigner, "credentials_expire", None)}
            if self.opts.on_demand_fallback and self.on_demand_price is None:
                self.say("the on-demand price is not known (the pricing service gave none and the config has none): "
                         "no on-demand fallback will be used")
            t = self.clock()
            inputs = self.bundle_inputs()
            self.aws.upload(inputs, self.key("inputs.tar"))
            self.timeline["upload"] = self.clock() - t
            t_launch = self.clock()
            self.limit_at = t_launch + (longest + 10 if self.opts.timeout_minutes is None else self.opts.timeout_minutes) * 60
            for job in self.jobs:
                th = threading.Thread(target=self._guard, args=(job,), name=f"job{job.index}", daemon=True)
                th.start()
                threads.append(th)
            limit = self.limit_at
            while any(th.is_alive() for th in threads):
                if self.clock() > limit:
                    raise RunFailed("the run exceeded its time limit")
                for th in threads:
                    th.join(0.2)
            self.timeline["jobs"] = self.clock() - t_launch
            failed = [r for r in self.results.values() if not r.ok]
            missing = len(self.results) < len(self.jobs)
            bad_shards = [r for r in failed if r.job.role == "shard"]
            shards_ok = [r for r in self.results.values() if r.ok and r.job.role == "shard"]
            if (bad_shards or missing) and self.opts.allow_incomplete and shards_ok:
                self.incomplete = True
                for r in bad_shards:
                    self.say(f"job {r.job.label} failed and is left out: {r.error}")
            elif bad_shards or missing:
                raise RunFailed("; ".join(f"job {r.job.label}: {r.error or 'no result'}" for r in failed)
                                or "a job never started")
            # A failed conformance run does not stop the benchmark shards from being merged; it fails the run after.
            self.deferred = [f"job {r.job.label}: {r.error}" for r in failed if r.job.role != "shard"]
        except BaseException as e:      # includes KeyboardInterrupt and the SIGTERM handler's exception
            error = e
        finally:
            t = self.clock()
            self.cancel.set()
            shield_interrupts()
            problems: list[str] = []
            try:
                for th in threads:
                    th.join(60)
                # Stop the billing first, then use the termination wait for the work that needs no instance.
                early = self.teardown.request()
                if error is None and self.after_collect:
                    tm = self.clock()
                    try:
                        self.after_collect(self)
                    except Exception as e:      # reported in the summary; the teardown must still run
                        self.extra["after_collect_error"] = repr(e)
                    self.timeline["merge"] = self.clock() - tm
                problems = early + self.teardown.run()
            except BaseException as e:          # the summary below must still be written
                problems.append(f"teardown: {e!r}")
            self.timeline["teardown"] = self.clock() - t - self.timeline.get("merge", 0.0)
        self.timeline["total"] = self.clock() - t0
        try:
            summary = self.summary(problems, error)
            self.write_summary(summary)
        except Exception as e:
            summary = {"run_id": self.run_id, "ok": False, "error": repr(error) if error else "",
                       "teardown_problems": problems, "summary_error": repr(e)}
            try:
                self.write_summary(summary)
            except OSError:
                pass
        if problems:
            self.say(f"teardown left something behind for run {self.run_id}: " + "; ".join(problems))
            self.say(f"finish it with: bin/bench-cloud cleanup --run {self.run_id}")
        if error is not None:
            raise error
        if self.incomplete:
            raise RunFailed("some jobs failed; the shards that succeeded were merged with --allow-incomplete")
        if self.deferred:
            raise RunFailed("; ".join(self.deferred))
        if problems:
            raise RunFailed("teardown left resources behind: " + "; ".join(problems))
        return summary

    def types(self) -> str:
        return ",".join(sorted({j.instance_type for j in self.jobs}))

    def _guard(self, job: Job) -> None:
        try:
            self.run_job(job)
        except BaseException as e:      # a crash in one thread fails its job, never the others' teardown
            with self.lock:
                res = self.results.setdefault(job.index, JobResult(job))
                res.error = f"internal error: {e!r}"

    def bundle_inputs(self) -> Path:
        self.opts.out.mkdir(parents=True, exist_ok=True)
        return packmod.bundle_inputs(self.packs, self.opts.out / "inputs.tar")

    def summary(self, problems: list[str], error: BaseException | None) -> dict:
        jobs = []
        total_cost = 0.0
        started = {"spot": 0, "on-demand": 0}
        by_life = {"spot": 0.0, "on-demand": 0.0}
        for idx in sorted(self.results):
            r = self.results[idx]
            cost = sum(self.cost_of(a) for a in r.attempts)
            total_cost += cost
            for a in r.attempts:
                if a.instance_id:
                    started[a.lifecycle] += 1
                    by_life[a.lifecycle] = round(by_life[a.lifecycle] + self.cost_of(a), 3)
            final = next((a for a in r.attempts if a.outcome == "done"), r.attempts[-1] if r.attempts else None)
            marks = (final.status.get("marks") if final else None) or {}
            jobs.append({
                "job": r.job.label, "ok": r.ok, "error": r.error, "instance_type": r.job.instance_type,
                "lifecycle": final.lifecycle if final else None,
                "attempts": [{"n": a.number, "lifecycle": a.lifecycle, "outcome": a.outcome, "detail": a.detail}
                             for a in r.attempts],
                "ran_on_demand": any(a.lifecycle == "on-demand" and a.outcome == "done" for a in r.attempts),
                "zone_requested": next((a.zone for a in r.attempts if a.zone and a.instance_id), ""),
                "availability_zone": ((final.status.get("instance") or {}).get("KIT_INSTANCE_AZ") if final else None),
                "governor": final.status.get("governor") if final else None,
                "seconds": self.phase_seconds(final, marks),
                "cost_estimate": round(cost, 3),
            })
        return {
            "run_id": self.run_id, "ok": error is None and not problems and not self.incomplete and not self.deferred,
            "incomplete": self.incomplete, "error": repr(error) if error else "",
            "teardown_problems": problems, "teardown_pending": self.teardown.pending,
            "extra": self.extra, "instance_type": self.types(), "image": self.image_id,
            "price_per_hour_spot": self.price, "price_per_hour_on_demand": self.on_demand_price,
            "signing": self.signing, "instances_started": started, "cost_by_lifecycle": by_life,
            "cost_estimate": round(total_cost, 3), "timeline_seconds": {k: round(v, 1) for k, v in self.timeline.items()},
            "inputs": packmod.describe(self.packs), "jobs": jobs,
        }

    @staticmethod
    def phase_seconds(att: Attempt | None, marks: dict) -> dict:
        """Where an instance's time went, from the clock marks it recorded."""
        if not att or not marks:
            return {}
        m = marks
        out = {}

        def span(name, a, b):
            if a is not None and b is not None and b >= a:
                out[name] = round(b - a, 1)
        span("api_to_kernel_boot", att.launched, m.get("boot"))
        span("boot_to_script", m.get("boot"), m.get("script_start"))
        span("setup", m.get("script_start"), m.get("setup_done"))
        span("inputs", m.get("setup_done"), m.get("inputs_ready"))
        span("cache_warm", m.get("inputs_ready"), m.get("cache_warm"))
        span("build", m.get("bench_start"), m.get("measuring"))
        span("measure", m.get("measuring"), m.get("bench_done"))
        span("launch_to_measuring", att.launched, m.get("measuring"))
        span("kernel_boot_to_measuring", m.get("boot"), m.get("measuring"))
        return out

    def write_summary(self, summary: dict) -> None:
        self.opts.out.mkdir(parents=True, exist_ok=True)
        (self.opts.out / "cloud-run.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")


def shield_interrupts() -> None:
    """A second Ctrl-C must not cut the teardown short."""
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except ValueError:      # not the main thread (tests)
        pass


def install_signal_handlers() -> None:
    """Turn SIGTERM and SIGHUP into the same exit path as Ctrl-C, so the teardown always runs."""
    def handler(signum, frame):
        raise KeyboardInterrupt(signal.Signals(signum).name)
    for s in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(s, handler)
        except ValueError:      # not the main thread
            return


def ensure_signal_handlers() -> None:
    """For code that is called without going through the command line (the image bake): install the handlers
    unless something already has."""
    try:
        if signal.getsignal(signal.SIGTERM) == signal.SIG_DFL:
            install_signal_handlers()
    except ValueError:
        pass


def merge(kit_root: Path, shard_dirs: list[Path], out: Path, say=print, allow_incomplete: bool = False) -> int:
    cmd = [sys.executable, str(kit_root / "bin" / "bench"), "merge", "--out", str(out)]
    if allow_incomplete:
        cmd.append("--allow-incomplete")
    cmd += [str(d) for d in shard_dirs]
    say("merging: bin/bench merge --out " + out.name + " " + " ".join(d.parent.name + "/" + d.name for d in shard_dirs))
    r = subprocess.run(cmd, capture_output=True, text=True)
    say((r.stdout + r.stderr).strip()[-1500:])
    return r.returncode
