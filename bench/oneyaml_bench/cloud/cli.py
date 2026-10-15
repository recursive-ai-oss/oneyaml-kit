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


def bench_command(mode: str, label: str, filters: list[str], pin: int) -> list[str]:
    """One shard's `bin/bench run`. The filters are the same ones the plan was made with: the plan digest covers them."""
    return ["bench", "run", "--mode", mode, "--shard", label, "--pin", str(pin), "--out", "{out}", *filters]


def filter_args(langs, tasks, libraries, workloads) -> list[str]:
    out: list[str] = []
    for flag, values in (("--lang", langs), ("--task", tasks), ("--library", libraries), ("--workload", workloads)):
        for v in values or []:
            out += [flag, v]
    return out


def own_adapters(kit_root: Path, langs: list[str] | None, available_env: set[str]) -> tuple[list[str], list[str]]:
    """The conformance adapters of our own libraries in the languages of the run, and the ones left out because
    their source is not available (the variable that would bring it in is not set)."""
    use, skipped = [], []
    for p in sorted((kit_root / "adapters").glob("*/adapter.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        if not d["name"].startswith("oneyaml-") or (langs and d.get("language") not in langs):
            continue
        need = [v for v in d.get("requires_env", []) if v not in available_env]
        if need:
            skipped.append(f"{d['name']} ({need[0]} is not set)")
        else:
            use.append(d["name"])
    return use, skipped


def conformance_job(index: int, itype: str, adapters: list[str]) -> runner.Job:
    cmds = [["kit", "run", "--slow", "--adapter", name, "--out", "{out}"] for name in adapters]
    return runner.Job(index, "conformance", itype, cmds, planmod.CONFORMANCE_LIFETIME_MINUTES,
                      planmod.conformance_instance_seconds(len(adapters)), role="conformance")


def jobs_from_plan(plan: planmod.Plan, itype: str, mode: str, filters: list[str], pin: int) -> list[runner.Job]:
    """One job per planned shard, all of one instance type."""
    jobs = []
    for s in plan.shards:
        label = f"{s.index}/{plan.count}"
        jobs.append(runner.Job(s.index, label, itype, [bench_command(mode, label, filters, pin)],
                               planmod.lifetime_minutes(s, mode), planmod.instance_seconds(s)))
    return jobs


def cmd_setup(a, cfg, aws, say) -> int:
    say("bucket created" if aws.ensure_bucket() else "bucket exists; settings refreshed")
    return 0


def cmd_image(a, cfg, aws, say) -> int:
    work = (Path(a.out) if a.out else default_out()) / "image"
    work.mkdir(parents=True, exist_ok=True)
    aws.tmpdir = work
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
    st, cl = summary.get("instances_started", {}), summary.get("cost_by_lifecycle", {})
    done = {"spot": 0, "on-demand": 0}
    for j in summary["jobs"]:
        if j["ok"] and j["lifecycle"] in done:
            done[j["lifecycle"]] += 1
    say(f"instances started: {st.get('spot', 0)} spot, {st.get('on-demand', 0)} on-demand; "
        f"jobs finished on: {done['spot']} spot, {done['on-demand']} on-demand")
    say(f"estimated cost ${summary['cost_estimate']:.2f} (spot ${cl.get('spot', 0):.2f}, on-demand "
        f"${cl.get('on-demand', 0):.2f}); spot ${summary['price_per_hour_spot']:.4f}/h with the volume; "
        f"signed with: {(summary.get('signing') or {}).get('mode', 'unknown')}")


class DryRun(Exception):
    """Raised after the plan, the estimate and the cost guard, when nothing is to be started."""


def merge_after_collect(out: Path):
    """The hook that merges the shards, and files the conformance results beside the merged benchmark result, in
    the layout `bin/bench readme --results` reads: bench/results.json and conformance/<library>.json. It runs
    while the instances are still finishing their termination."""
    def hook(r: runner.Run) -> None:
        shard_dirs = [Path(res.out_dir) / "0" for _, res in sorted(r.results.items())
                      if res.out_dir and res.job.role == "shard"]
        final = out / "results"
        r.extra["merge_exit"] = runner.merge(KIT_ROOT, shard_dirs, final / "bench", r.say, allow_incomplete=r.incomplete)
        for _, res in sorted(r.results.items()):
            if res.out_dir and res.job.role == "conformance":
                for k in sorted(p for p in Path(res.out_dir).iterdir() if p.is_dir()):
                    for f in sorted((k / "results").glob("*.json")):
                        (final / "conformance").mkdir(parents=True, exist_ok=True)
                        (final / "conformance" / f.name).write_bytes(f.read_bytes())
                        r.say(f"conformance result: results/conformance/{f.name}")
    return hook


def prepare_run(a, cfg, aws, say, itype: str, jobs_for, merge: bool = False) -> tuple[runner.Run, Path]:
    out = Path(a.out) if a.out else default_out()
    run_id = "r" + time.strftime("%m%d%H%M%S") + os.urandom(2).hex()
    out = out / run_id
    out.mkdir(parents=True, exist_ok=True)
    aws.tmpdir = out        # private request files live with the run, not in a shared directory
    spot = aws.spot_price(itype)
    od_list = aws.on_demand_price(itype) or cfg.on_demand_price
    # The volume is billed for the life of the instance; every figure below includes it.
    vol = planmod.volume_per_hour(cfg.volume_gb)
    price = spot + vol
    od = od_list + vol if od_list else None
    jobs, est = jobs_for(price)
    cap = a.cost_cap if a.cost_cap is not None else cfg.cost_cap
    say(f"{itype}: spot ${spot:.4f}/h (dearest zone{' of ' + ','.join(cfg.zones) if cfg.zones else ''})"
        + (f", on-demand ${od_list:.4f}/h" if od_list else ", on-demand price unknown: no on-demand fallback")
        + f", volume ${vol:.4f}/h; estimated cost ${est:.2f}, cap ${cap:.2f}")
    if est > cap:
        raise runner.RunFailed(f"the estimated cost ${est:.2f} is above the cap ${cap:.2f}; raise it with --cost-cap")
    if getattr(a, "dry_run", False):
        raise DryRun()
    image_id, info = imagemod.ensure_image(aws, cfg, KIT_ROOT, out / "image", say)
    t = time.time()
    packs = packmod.pack_all(KIT_ROOT, out / "packs", kit_tree=a.kit_tree)
    for p in packs:
        say(f"packed {p.name} {p.revision[:12]}{' (uncommitted changes not included)' if p.dirty and p.source == 'git' else ''}"
            f"{' (working tree)' if p.source == 'tree' else ''}, {p.size} bytes")
    opts = runner.Options(out=out, mode=getattr(a, "mode", "full"), cost_cap=cap, volume_gb=cfg.volume_gb,
                          allow_incomplete=getattr(a, "allow_incomplete", False),
                          timeout_minutes=a.timeout_minutes, spot_retries=a.spot_retries,
                          on_demand_fallback=not a.no_on_demand)
    r = runner.Run(aws, cfg, opts, jobs, image_id, packs, price, od, say=say, run_id=run_id,
                   after_collect=merge_after_collect(out) if merge else None)
    r.extra["volume_per_hour"] = round(vol, 4)
    r.timeline["pack"] = time.time() - t
    if info["baked"]:
        r.timeline["image_bake"] = info["seconds"]
    return r, out


def execute(r: runner.Run, aws, say, out: Path) -> dict:
    """Runs with the teardown wired to every way out; returns the summary."""
    runner.ensure_signal_handlers()
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
    filters = filter_args(a.lang, a.task, a.library, a.workload)
    vcpus = aws.vcpus(itype)
    adapters, skipped = ([], [])
    if a.conformance:
        adapters, skipped = own_adapters(KIT_ROOT, a.lang, {v for v in packmod.LIBRARY_VARS if os.environ.get(v)})
    for s in skipped:
        say(f"conformance left out for {s}")
    if a.conformance and not adapters:
        say("conformance: no library of ours is available for this selection; no conformance job")
    extra_jobs = 1 if adapters else 0
    max_n = max(1, min(cfg.max_shards, cfg.spot_vcpu_quota // vcpus - extra_jobs))
    cap = a.cost_cap if a.cost_cap is not None else cfg.cost_cap

    def jobs_for(price):
        def get(k):
            return planmod.run_planner(KIT_ROOT, k, a.mode, filters)
        if a.shards == "auto":
            n, plan, table = planmod.choose_shards(get, max_n, price, cap, len(adapters))
            for row in table:
                say(f"  candidate {row['shards']} shards: wall {row['wall_seconds']} s, cost ${row['cost']:.2f}")
        else:
            n = int(a.shards)
            if n > max_n:
                raise runner.RunFailed(f"{n} shards and {extra_jobs} conformance job of {itype} ({vcpus} vCPUs each) exceed "
                                       f"the spot quota of {cfg.spot_vcpu_quota} vCPUs; at most {max_n} shards")
            plan = get(n)
        say(f"plan {plan.digest}: {plan.count} shard(s)")
        for s in plan.shards:
            say(f"  shard {s.index}/{plan.count}: {len(s.groups)} groups, about {round(s.seconds)} s of measuring, "
                f"{round(planmod.instance_seconds(s))} s on the instance, limit {planmod.lifetime_minutes(s, a.mode)} min")
        jobs = jobs_from_plan(plan, itype, a.mode, filters, a.pin)
        if adapters:
            jobs.append(conformance_job(plan.count + 1, itype, adapters))
            say("conformance job: " + ", ".join(adapters))
        return jobs, planmod.plan_cost(plan, price, len(adapters))

    try:
        r, out = prepare_run(a, cfg, aws, say, itype, jobs_for, merge=True)
    except DryRun:
        return 0
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
    rc = r.extra.get("merge_exit")
    if rc is not None:
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
            cmds.append(["bench", "run", "--mode", "quick", "--pin", str(a.pin), "--out", "{out}", *sel])
        jobs = [runner.Job(i, t, t, cmds, planmod.QUICK_LIFETIME_MINUTES, 600) for i, t in enumerate(types, 1)]
        vol = planmod.volume_per_hour(cfg.volume_gb)
        return jobs, sum(planmod.shard_cost(aws.spot_price(t) + vol, 600) for t in types)

    r, out = prepare_run(a, cfg, aws, say, types[0], jobs_for)
    r.prices = {t: aws.spot_price(t) + planmod.volume_per_hour(cfg.volume_gb) for t in types}
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
    # Images are removed only on request: another checkout of the kit may use the image this one would call old.
    digest = imagemod.current_digest(cfg, KIT_ROOT) if a.images and not a.run else None
    left = cleanup_mod.cleanup(aws, a.run, say, wait=a.wait, keep_image_digest=digest)
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
    s.add_argument("--task", action="append", help="measure only this task (repeatable); the plan sees it too")
    s.add_argument("--library", action="append", help="measure only this library (repeatable)")
    s.add_argument("--workload", action="append", help="measure only this workload (repeatable)")
    s.add_argument("--allow-incomplete", action="store_true",
                   help="merge the shards that succeeded when others failed; the result is never publishable and the run fails")
    s.add_argument("--dry-run", action="store_true", help="print the plan, the estimates and the cost check, then stop")
    s.add_argument("--conformance", action=argparse.BooleanOptionalAction, default=True,
                   help="also run the full conformance of our own libraries in the selected languages (default: on)")
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
    s.add_argument("--run", help="only this run id, whatever its deadline; without it, only runs past their deadline")
    s.add_argument("--wait", type=float, default=900.0, help="seconds to wait for instances to finish terminating")
    s.add_argument("--images", action="store_true",
                   help="also remove machine images of another digest that are available and older than the "
                        "retention period (7 days); never the current one or one an instance uses")
    s.set_defaults(fn=cmd_cleanup)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    say = Stamp()
    # Before any command: setup, image and cleanup start instances or delete things too, and a signal must
    # reach their `finally` blocks instead of killing the process outright.
    runner.install_signal_handlers()
    try:
        cfg = configmod.load()
        aws = make_aws(cfg)
        return a.fn(a, cfg, aws, say)
    except (configmod.ConfigError, runner.RunFailed, planmod.PlanError, packmod.PackError, AwsError) as e:
        say(f"bench-cloud: {e}")
        return 1
    except KeyboardInterrupt:
        say("bench-cloud: interrupted")
        return 130
