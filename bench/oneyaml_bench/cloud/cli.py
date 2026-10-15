"""bin/bench-cloud: run the timed benchmarks on short-lived EC2 spot instances.

    bin/bench-cloud setup                    create the private bucket (once)
    bin/bench-cloud image [--force]          bake the machine image, or reuse the current one
    bin/bench-cloud run --shards N|auto ...  measure on N instances, merge, tear everything down
    bin/bench-cloud spread --types A,B       compare the run-to-run spread of instance types
    bin/bench-cloud cleanup [--run ID]       terminate and delete every tagged leftover
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import statistics
import time
from pathlib import Path

from . import cleanup as cleanup_mod, config as configmod, image as imagemod, pack as packmod, plan as planmod, runner
from .aws import Aws, AwsError

KIT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PIN = 2


class Stamp:
    """Prints lines with the time since the start, so the log shows where the wall-clock time went."""

    def __init__(self):
        self.t0 = time.monotonic()

    def __call__(self, msg: str) -> None:
        s = time.monotonic() - self.t0
        print(f"[{int(s // 60):02d}:{s % 60:04.1f}] {msg}", flush=True)


def default_out() -> Path:
    return Path(os.environ.get("KIT_OUT", KIT_ROOT / "out")) / "cloud"


def make_aws(cfg) -> Aws:
    return Aws(cfg)


def bench_command(mode: str, label: str, langs: list[str] | None, pin: int, extra: list[str]) -> list[str]:
    cmd = ["run", "--mode", mode, "--shard", label, "--pin", str(pin), "--out", "{out}"]
    for lang in langs or []:
        cmd += ["--lang", lang]
    return cmd + extra


def jobs_from_plan(plan: planmod.Plan, itype: str, mode: str, langs: list[str] | None, pin: int, extra: list[str]) -> list[runner.Job]:
    """One job per planned shard, all of one instance type. A quick run's time is capped: it only checks the pipeline."""
    jobs = []
    for s in plan.shards:
        secs = min(s.seconds, 300) if mode == "quick" else s.seconds
        label = f"{s.index}/{plan.count}"
        jobs.append(runner.Job(s.index, label, itype, [bench_command(mode, label, langs, pin, extra)],
                               planmod.lifetime_minutes(secs, mode), secs))
    return jobs


def cmd_setup(a, cfg, aws, say) -> int:
    say("bucket created" if aws.ensure_bucket() else "bucket exists; settings refreshed")
    return 0


def cmd_image(a, cfg, aws, say) -> int:
    work = (Path(a.out) if a.out else default_out()) / "image"
    t0 = time.time()
    image_id, info = imagemod.ensure_image(aws, cfg, KIT_ROOT, work, say, force=a.force)
    say(f"image {image_id} ({'baked in %d s' % info['seconds'] if info['baked'] else 'reused'}), digest {info['digest']}")
    print(json.dumps({"image": image_id, **info, "wall_seconds": round(time.time() - t0)}))
    return 0


def run_summary(say, summary: dict) -> None:
    say("---- summary")
    say(f"instance type {summary['instance_type']}, run ok: {summary['ok']}")
    for j in summary["jobs"]:
        s = j["seconds"]
        say(f"job {j['job']}: {'ok' if j['ok'] else 'FAILED ' + j['error']}; {j['lifecycle']}"
            f"{' (on-demand fallback)' if j['ran_on_demand'] else ''}; zone {j['availability_zone']}; "
            f"governor {j['governor']}; attempts {len(j['attempts'])}")
        if s:
            say("   " + ", ".join(f"{k} {v}s" for k, v in s.items()))
    say("timeline: " + ", ".join(f"{k} {v}s" for k, v in summary["timeline_seconds"].items()))
    say(f"estimated cost ${summary['cost_estimate']:.2f} (spot ${summary['price_per_hour_spot']:.4f}/h)")


def merge_after_collect(out: Path):
    """The hook that merges the shards while the instances are still finishing their termination."""
    def hook(r: runner.Run) -> None:
        dirs = [Path(res.out_dir) / "0" for _, res in sorted(r.results.items()) if res.out_dir]
        r.extra["merge_exit"] = runner.merge(KIT_ROOT, dirs, out / "merged", r.say)
    return hook


def prepare_run(a, cfg, aws, say, itype: str, jobs_for, merge: bool = False) -> tuple[runner.Run, Path]:
    out = Path(a.out) if a.out else default_out()
    run_id = "r" + time.strftime("%m%d%H%M%S") + os.urandom(2).hex()
    out = out / run_id
    out.mkdir(parents=True, exist_ok=True)
    price = aws.spot_price(itype)
    od = aws.on_demand_price(itype)
    jobs, est = jobs_for(price)
    cap = a.cost_cap if a.cost_cap is not None else cfg.cost_cap
    say(f"{itype}: spot ${price:.4f}/h" + (f", on-demand ${od:.4f}/h" if od else "") + f"; estimated cost ${est:.2f}, cap ${cap:.2f}")
    if est > cap:
        raise runner.RunFailed(f"the estimated cost ${est:.2f} is above the cap ${cap:.2f}; raise it with --cost-cap")
    image_id, info = imagemod.ensure_image(aws, cfg, KIT_ROOT, out / "image", say)
    t = time.time()
    packs = packmod.pack_all(KIT_ROOT, out / "packs", kit_tree=a.kit_tree)
    for p in packs:
        say(f"packed {p.name} {p.revision[:12]}{' (uncommitted changes not included)' if p.dirty and p.source == 'git' else ''}"
            f"{' (working tree)' if p.source == 'tree' else ''}, {p.size} bytes")
    opts = runner.Options(out=out, mode=getattr(a, "mode", "full"), cost_cap=cap, volume_gb=cfg.volume_gb,
                          timeout_minutes=a.timeout_minutes, spot_retries=a.spot_retries,
                          on_demand_fallback=not a.no_on_demand)
    r = runner.Run(aws, cfg, opts, jobs, image_id, packs, price, od, say=say, run_id=run_id,
                   after_collect=merge_after_collect(out) if merge else None)
    r.timeline["pack"] = time.time() - t
    if info["baked"]:
        r.timeline["image_bake"] = info["seconds"]
    return r, out


def execute(r: runner.Run, aws, say, out: Path) -> dict:
    """Runs with the teardown wired to every way out; returns the summary."""
    runner.install_signal_handlers()
    atexit.register(lambda: r.teardown.done or r.teardown.run(quiet=True))
    try:
        summary = r.execute()
    except BaseException as e:
        summary = json.loads((out / "cloud-run.json").read_text()) if (out / "cloud-run.json").exists() else None
        if summary:
            run_summary(say, summary)
        raise e
    run_summary(say, summary)
    return summary


def verify_clean(aws, run_id: str, say) -> int:
    """Fails when anything tagged still runs. An instance that is shutting down is billed no more; the cleanup
    command finishes it off."""
    left = cleanup_mod.cleanup(aws, run_id, say, wait=0)
    if cleanup_mod.total(left):
        say("still shutting down (not billed); `bin/bench-cloud cleanup` removes the rest: " + json.dumps(left))
    return 3 if cleanup_mod.still_running(left) else 0


def cmd_run(a, cfg, aws, say) -> int:
    itype = a.instance_type or cfg.instance_type
    langs = a.lang or None
    vcpus = aws.vcpus(itype)
    max_n = max(1, min(cfg.max_shards, cfg.spot_vcpu_quota // vcpus))
    extra = a.bench_arg or []

    def jobs_for(price):
        if a.shards == "auto":
            n, plan, table = planmod.choose_shards(lambda k: planmod.run_planner(KIT_ROOT, k, langs), max_n, price,
                                                   a.cost_cap if a.cost_cap is not None else cfg.cost_cap)
            for row in table:
                say(f"  candidate {row['shards']} shards: wall {row['wall_seconds']} s, cost ${row['cost']:.2f}")
        else:
            n = int(a.shards)
            if n > max_n:
                raise runner.RunFailed(f"{n} instances of {itype} ({vcpus} vCPUs each) exceed the spot quota of "
                                       f"{cfg.spot_vcpu_quota} vCPUs; at most {max_n}")
            plan = planmod.run_planner(KIT_ROOT, n, langs)
        say(f"plan {plan.digest}: {plan.count} shard(s)")
        jobs = jobs_from_plan(plan, itype, a.mode, langs, a.pin, extra)
        return jobs, planmod.plan_cost(planmod.Plan(plan.digest, [planmod.ShardPlan(j.index, j.est_seconds, []) for j in jobs]), price)

    r, out = prepare_run(a, cfg, aws, say, itype, jobs_for, merge=True)
    code = 0
    try:
        summary = execute(r, aws, say, out)
    except runner.RunFailed as e:
        say(f"run failed: {e}")
        code = 1
        summary = None
    except KeyboardInterrupt:
        say("interrupted; everything tagged with this run was terminated")
        code = 130
        summary = None
    if summary:
        rc = r.extra.get("merge_exit")
        code = code or (0 if rc == 0 else 1)
    clean = verify_clean(aws, r.run_id, say)
    return code or clean


def parse_types(s: str) -> list[str]:
    return [t.strip() for t in s.split(",") if t.strip()]


def cmd_spread(a, cfg, aws, say) -> int:
    """Runs the same few quick cells several times on each type, and compares their run-to-run spread."""
    types = parse_types(a.types)
    sel = []
    for flag, value in (("--lang", a.lang), ("--task", a.task), ("--library", a.library), ("--workload", a.workload)):
        for v in (value or "").split(","):
            if v:
                sel += [flag, v]
    a.mode = "quick"

    def jobs_for(price):
        cmds = []
        for _ in range(a.repeat):
            cmds.append(["run", "--mode", "quick", "--pin", str(a.pin), "--out", "{out}", *sel])
        jobs = [runner.Job(i, t, t, cmds, planmod.QUICK_LIFETIME_MINUTES, 600) for i, t in enumerate(types, 1)]
        return jobs, sum(planmod.shard_cost(aws.spot_price(t), 600) for t in types)

    r, out = prepare_run(a, cfg, aws, say, types[0], jobs_for)
    r.prices = {t: aws.spot_price(t) for t in types}
    # Different types in one run are the point here, so the price used for the estimate is each type's own.
    code = 0
    try:
        summary = execute(r, aws, say, out)
    except (runner.RunFailed, KeyboardInterrupt) as e:
        say(f"spread run failed: {e!r}")
        code, summary = 1, None
    if summary:
        report = spread_report(r, a.repeat)
        (out / "spread.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=1))
    return code or verify_clean(aws, r.run_id, say)


def cell_key(c: dict) -> str:
    return f"{c['language']}/{c['library']}/{c['task']}/{c['workload']}"


def spread_report(r: runner.Run, repeat: int) -> dict:
    out = {}
    for idx, res in sorted(r.results.items()):
        runs = []
        for k in range(repeat):
            p = Path(res.out_dir) / str(k) / "results.json"
            if p.exists():
                runs.append(json.loads(p.read_text(encoding="utf-8")))
        by: dict[str, list[float]] = {}
        within: dict[str, list[float]] = {}
        for rr in runs:
            for c in rr["cells"]:
                if c.get("status") == "ok" and c.get("time"):
                    by.setdefault(cell_key(c), []).append(c["time"]["median_ns"])
                    within.setdefault(cell_key(c), []).append(c["time"]["mad_ns"] / c["time"]["median_ns"])
        cells = {}
        for key, vals in by.items():
            if len(vals) > 1:
                cells[key] = {"runs": len(vals), "median_ns": statistics.median(vals),
                              "between_runs_cv_pct": round(100 * statistics.pstdev(vals) / statistics.mean(vals), 2),
                              "range_pct": round(100 * (max(vals) - min(vals)) / statistics.median(vals), 2),
                              "within_run_mad_pct": round(100 * statistics.median(within[key]), 2)}
        env = runs[0]["run"]["environment"] if runs else {}
        cvs = [c["between_runs_cv_pct"] for c in cells.values()]
        job = res.job
        final = next((x for x in res.attempts if x.outcome == "done"), None)
        out[job.instance_type] = {
            "cpu": env.get("cpu"), "cpus": env.get("cpus"), "governor": env.get("governor"), "boost": env.get("boost"),
            "os": env.get("os"), "cells": cells,
            "median_between_runs_cv_pct": round(statistics.median(cvs), 2) if cvs else None,
            "max_between_runs_cv_pct": max(cvs) if cvs else None,
            "lifecycle": final.lifecycle if final else None,
            "boot_to_measuring_s": (final.status.get("marks", {}).get("measuring", 0) - final.status.get("marks", {}).get("boot", 0))
            if final else None,
        }
    return out


def cmd_cleanup(a, cfg, aws, say) -> int:
    left = cleanup_mod.cleanup(aws, a.run, say, wait=a.wait)
    return 0 if cleanup_mod.total(left) == 0 else 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench-cloud", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="create the private bucket with its expiry rule")
    s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("image", help="bake the machine image, or reuse the one that matches the toolchains and lock files")
    s.add_argument("--force", action="store_true", help="bake even when a matching image exists")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_image)

    def common(s):
        s.add_argument("--out")
        s.add_argument("--instance-type")
        s.add_argument("--cost-cap", type=float, help="refuse to start above this estimated cost in dollars (default from the config)")
        s.add_argument("--kit-tree", action="store_true", help="pack the kit's working tree instead of its HEAD")
        s.add_argument("--timeout-minutes", type=int)
        s.add_argument("--spot-retries", type=int, default=2)
        s.add_argument("--no-on-demand", action="store_true", help="never fall back to on-demand")
        s.add_argument("--pin", type=int, default=DEFAULT_PIN, help="CPU the harness is pinned to")

    s = sub.add_parser("run", help="measure on spot instances, merge the shards, terminate everything")
    s.add_argument("--shards", default="auto", help="a number, or auto: the shortest wall-clock time under the cost cap")
    s.add_argument("--lang", action="append")
    s.add_argument("--mode", choices=("quick", "full"), default="full")
    s.add_argument("--bench-arg", action="append", help="an extra argument for every `bench run` (repeatable)")
    common(s)
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("spread", help="repeat a few quick cells on each instance type and compare the spread")
    s.add_argument("--types", required=True, help="comma-separated instance types")
    s.add_argument("--repeat", type=int, default=5)
    s.add_argument("--lang", default="go,rust", help="comma-separated")
    s.add_argument("--task", default="generic-load,dump", help="comma-separated")
    s.add_argument("--library", default="", help="comma-separated; default all")
    s.add_argument("--workload", default="", help="comma-separated; default all")
    common(s)
    s.set_defaults(fn=cmd_spread)
    s = sub.add_parser("cleanup", help="terminate and delete every tagged leftover, then show what remains")
    s.add_argument("--run", help="only this run id")
    s.add_argument("--wait", type=float, default=900.0, help="seconds to wait for instances to finish terminating")
    s.set_defaults(fn=cmd_cleanup)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    say = Stamp()
    try:
        cfg = configmod.load()
        aws = make_aws(cfg)
        return a.fn(a, cfg, aws, say)
    except (configmod.ConfigError, runner.RunFailed, planmod.PlanError, packmod.PackError, AwsError) as e:
        say(f"bench-cloud: {e}")
        return 1
