"""Splitting one benchmark run across identical machines, and putting the pieces back together.

A comparison row is every library of one language measured on one task and workload, and a row
only means something when all its libraries ran on the same machine. So the unit of work is a
*group*: one language on one workload, with all its libraries and tasks. A plan assigns every
group to exactly one of N shards. Each machine computes the plan from the same inputs, so no
two of them need to talk, and the plan carries a digest that the merge compares.

The plan is built from what is known before anything is built: the languages that have a
harness, the workloads with their sizes and tasks, and the filters of the run. The libraries of
a harness are known only once it is built, so they are not in the plan; every group runs all
of its language's libraries, and the merge checks that all shards saw the same ones.

`merge` joins the shard results into one result that is exactly as publishable as an unsharded
run of the same selection: it re-checks every shard against the publishing rule, and it is
never more publishable than its worst shard.
"""

from __future__ import annotations

import hashlib
import json

from . import results, workloads

FORMAT = "oneyaml-kit-bench-plan/1"

# Relative cost of a group: (input bytes + a fixed share) x tasks x passes. Every cell has a
# fixed cost (warm-up, sampling) whatever the input size, which the fixed share stands for; a
# harness that repeats the run to measure memory in several processes (see run.py) adds a
# quarter of a pass for each repeat, because those passes skip the timing. The numbers only
# balance the shards: they are relative, not seconds.
FIXED_BYTES = 256 * 1024
PASS_QUARTERS = 4


class PlanError(Exception):
    pass


class MergeError(Exception):
    """The shard results cannot be combined; `problems` lists why."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# ---- the plan ------------------------------------------------------------------------------

def group_id(language: str, workload: str) -> str:
    return f"{language}/{workload}"


def offered_workloads(tiers: tuple[str, ...]) -> list[dict]:
    """Every workload of the tiers with its input size and tasks, without writing any file."""
    tasks = workloads.universe(tiers)
    sizes = {}
    for m in workloads.load_real():
        if m.get("tier", "quick") in tiers:
            sizes[m["id"]] = (m["_dir"] / "input.yaml").stat().st_size
    for w in workloads.generated_workloads(tiers):
        sizes[w["id"]] = w["size"]
    for ref in ("reference-a", "reference-b"):
        sizes[ref] = len(workloads.REF_INPUT.encode("utf-8"))
    return [{"id": wid, "bytes": sizes[wid], "tasks": ts} for wid, ts in sorted(tasks.items())]


def language_passes(harness: dict, mode: str) -> int:
    """The cost of a language's pass over a group, in quarters of a timing pass."""
    n = max(int((harness.get("memory_processes") or {}).get(mode, 1)), 1)
    return PASS_QUARTERS + (n - 1)


def build_plan(passes: dict[str, int], offered: list[dict], count: int, tiers: tuple[str, ...],
               filters: dict) -> dict:
    """Assigns every (language, workload) group to a shard, balancing the estimated cost.

    Longest group first onto the cheapest shard so far; ties go to the lower id and the lower
    shard, so the result depends only on the inputs.
    """
    if count < 1:
        raise PlanError("the number of shards must be at least 1")
    groups = {}
    for lang in sorted(passes):
        for w in offered:
            ts = [t for t in w["tasks"] if not filters["tasks"] or t in filters["tasks"]]
            if (filters["workloads"] and w["id"] not in filters["workloads"]) or not ts:
                continue
            cost = (w["bytes"] + FIXED_BYTES) * len(ts) * passes[lang]
            groups[group_id(lang, w["id"])] = {"cost": cost, "tasks": ts}
    if not groups:
        raise PlanError("nothing to measure: no group is left after the filters")
    if count > len(groups):
        raise PlanError(f"{count} shards but only {len(groups)} comparison rows to share out")
    load = [0] * count
    for gid in sorted(groups, key=lambda g: (-groups[g]["cost"], g)):
        i = min(range(count), key=lambda k: (load[k], k))
        groups[gid]["shard"] = i + 1
        load[i] += groups[gid]["cost"]
    plan = {"format": FORMAT, "count": count, "tiers": list(tiers), "filters": filters, "groups": groups}
    plan["plan_digest"] = digest_of(plan)
    return plan


def digest_of(plan: dict) -> str:
    """A hash of everything that decides who measures what."""
    body = {k: plan[k] for k in ("count", "tiers", "filters", "groups")}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def plan_for(harnesses: dict[str, dict], languages: list[str], mode: str, libraries: list[str],
             tasks: list[str], only: list[str], count: int) -> dict:
    tiers = ("quick",) if mode == "quick" else ("quick", "full")
    return build_plan({l: language_passes(harnesses[l], mode) for l in languages},
                      offered_workloads(tiers), count, tiers,
                      {"libraries": sorted(libraries), "tasks": sorted(tasks), "workloads": sorted(only)})


def plan_mismatch(plan: dict, metas: list[dict], tasks: list[str]) -> str | None:
    """Why the plan does not fit the workloads actually prepared, or None. The plan is made from
    static data; a difference means the shards would not cover the rows they promise."""
    by_id = {m["id"]: [t for t in m["tasks"] if not tasks or t in tasks] for m in metas}
    for gid, g in plan["groups"].items():
        wid = gid.split("/", 1)[1]
        if by_id.get(wid) != g["tasks"]:
            return f"the shard plan disagrees with the prepared workload {wid!r} (tasks {by_id.get(wid)} against {g['tasks']})"
    return None


def shard_groups(plan: dict, index: int) -> list[str]:
    return sorted(g for g, v in plan["groups"].items() if v["shard"] == index)


def shard_workloads(plan: dict, index: int) -> dict[str, list[str]]:
    """language -> the workloads this shard measures for it."""
    out: dict[str, list[str]] = {}
    for gid in shard_groups(plan, index):
        lang, wid = gid.split("/", 1)
        out.setdefault(lang, []).append(wid)
    return out


def shard_record(plan: dict, index: int) -> dict:
    """What a shard's result carries so that the merge can check it without the plan."""
    return {"index": index, "count": plan["count"], "plan_digest": plan["plan_digest"],
            "plan": {k: plan[k] for k in ("tiers", "filters", "groups")}}


def describe(plan: dict) -> dict:
    """The plan as printed by `bench plan`."""
    shards_ = []
    total = sum(g["cost"] for g in plan["groups"].values())
    for i in range(1, plan["count"] + 1):
        gs = shard_groups(plan, i)
        cost = sum(plan["groups"][g]["cost"] for g in gs)
        shards_.append({"index": i, "groups": gs, "estimated_cost": cost, "share": round(cost / total, 4)})
    return {"format": FORMAT, "plan_digest": plan["plan_digest"], "count": plan["count"], "tiers": plan["tiers"],
            "filters": plan["filters"], "cost_unit": "relative; (input bytes + fixed share) x tasks x passes",
            "groups": len(plan["groups"]), "shards": shards_}


# ---- the merge -----------------------------------------------------------------------------

# Environment fields that may differ between machines of one kind: when the run started, and which
# core the harness was pinned to (the publishing rule needs a pin, not a particular core).
ENV_FREE = ("date", "pinned_cpu")


def _differs(items: list, what: str, problems: list[str]) -> bool:
    """Records `what` as a problem when the values are not all equal."""
    first = items[0]
    if all(x == first for x in items[1:]):
        return False
    problems.append(what)
    return True


def _env_problems(runs: list[dict]) -> list[str]:
    problems = []
    envs = [r["environment"] for r in runs]
    if len({e.get("kit_revision") for e in envs}) > 1:
        problems.append("the shards ran different kit revisions: "
                        + ", ".join(sorted({str(e.get("kit_revision")) for e in envs})))
    keys = sorted({k for e in envs for k in e} - set(ENV_FREE) - {"kit_revision"})
    for k in keys:
        vals = [e.get(k) for e in envs]
        if any(v != vals[0] for v in vals[1:]):
            shown = ", ".join(sorted({str(v) for v in vals}))
            problems.append(f"the machines differ in environment.{k}: {shown}")
    return problems


def merge(parts: list[dict], allow_incomplete: bool = False) -> dict:
    """One result from the results of the shards of one plan.

    Raises MergeError, and builds nothing, unless the shards are of one plan, are all there
    (unless `allow_incomplete`, which leaves the missing rows out and says so), and come from
    the same kit revision, harness pins and configuration, and the same kind of machine.
    """
    problems: list[str] = []
    if not parts:
        raise MergeError(["no shard results to merge"])
    for i, p in enumerate(parts):
        try:
            results.validate(p)
        except results.ResultError as e:
            problems.append(f"result {i + 1} is not a valid result: {e}")
        if "shard" not in p.get("run", {}):
            problems.append(f"result {i + 1} is not a shard (it has no run.shard)")
    if problems:
        raise MergeError(problems)
    recs = [p["run"]["shard"] for p in parts]
    runs = [p["run"] for p in parts]

    # One plan.
    if len({r["plan_digest"] for r in recs}) > 1:
        raise MergeError(["the shards come from different plans (plan digests differ): "
                          + ", ".join(sorted({r["plan_digest"][:19] for r in recs}))])
    plan = dict(recs[0]["plan"], count=recs[0]["count"])
    if digest_of(plan) != recs[0]["plan_digest"]:
        problems.append("the plan recorded in the shards does not match its digest")
    count = recs[0]["count"]
    if any(r["count"] != count for r in recs):
        problems.append("the shards disagree about how many shards there are")
    seen: dict[int, int] = {}
    for r in recs:
        seen[r["index"]] = seen.get(r["index"], 0) + 1
    dup = sorted(i for i, n in seen.items() if n > 1)
    if dup:
        problems.append("shard " + ", ".join(map(str, dup)) + " is given more than once")
    out_of_range = sorted(i for i in seen if not 1 <= i <= count)
    if out_of_range:
        problems.append(f"shard index {out_of_range} is outside 1..{count}")
    missing = sorted(set(range(1, count + 1)) - set(seen))
    if missing and not allow_incomplete:
        problems.append("shard " + ", ".join(map(str, missing)) + f" of {count} is missing")

    # The same code, pins and configuration, on the same kind of machine.
    problems += _env_problems(runs)
    _differs([r["mode"] for r in runs], "the shards ran in different modes", problems)
    _differs([bool(r.get("counters")) for r in runs], "the shards differ in whether they counted instructions",
             problems)
    langs: dict[str, dict] = {}
    for p in parts:
        for lang, h in p["harnesses"].items():
            if lang in langs and langs[lang] != h:
                problems.append(f"the {lang} harness differs between shards (version, libraries, pins, flags or configuration)")
            langs.setdefault(lang, h)
    wls: dict[str, dict] = {}
    for p in parts:
        for w in p["workloads"]:
            if w["id"] in wls and wls[w["id"]] != w:
                problems.append(f"workload {w['id']} differs between shards (the input changed)")
            wls.setdefault(w["id"], w)
    sels = [r.get("selection") or {} for r in runs]
    for k in ("languages", "libraries", "tasks", "workloads"):
        _differs([s.get(k) for s in sels], f"the shards were narrowed differently ({k})", problems)
    mps = [r.get("memory_processes") or {} for r in runs]
    for lang in sorted({l for m in mps for l in m}):
        if len({json.dumps(m[lang], sort_keys=True) for m in mps if lang in m}) > 1:
            problems.append(f"the shards measured {lang} memory in different numbers of processes")

    # Every shard holds exactly the rows the plan gave it, and measured nothing else.
    for p, r in zip(parts, recs):
        mine = {g for g, v in plan["groups"].items() if v["shard"] == r["index"]}
        claimed = set((p["run"].get("selection") or {}).get("groups", []))
        if claimed != mine:
            problems.append(f"shard {r['index']} does not hold the rows the plan gave it")
        stray = {f"{c['language']}/{c['workload']}" for c in p["cells"]} - mine
        if stray:
            problems.append(f"shard {r['index']} has cells outside its rows ({', '.join(sorted(stray)[:3])})")
    if problems:
        raise MergeError(problems)

    parts = sorted(parts, key=lambda p: p["run"]["shard"]["index"])
    runs = [p["run"] for p in parts]
    held = {g for p in parts for g in p["run"]["selection"]["groups"]}
    absent = sorted(set(plan["groups"]) - held)
    left_out = {}
    for kind in ("libraries", "tasks", "workloads"):
        left_out[kind] = sorted({n for r in runs for n in r["selection"]["left_out"].get(kind, [])})
    left_out["groups"] = absent
    sel = {k: runs[0]["selection"][k] for k in ("languages", "libraries", "tasks", "workloads")}
    sel["groups"] = sorted(held)
    sel["left_out"] = left_out

    cells = [c for p in parts for c in p["cells"]]
    pub = _merged_publishability(parts, left_out)
    shard_rows = []
    for p in parts:
        r = p["run"]
        shard_rows.append({"index": r["shard"]["index"], "groups": r["selection"]["groups"], "date": r["date"],
                           "seconds": r["seconds"], "restarts": r["restarts"], "environment": r["environment"],
                           "load": r["load"], "measured": r["publishability"]["measured"]})
    run = {
        "date": min(r["date"] for r in runs), "mode": runs[0]["mode"],
        "seconds": max(r["seconds"] for r in runs), "restarts": sum(r["restarts"] for r in runs),
        "counters": runs[0]["counters"], "environment": runs[0]["environment"], "publishability": pub,
        "selection": sel,
        "load": {"before": max(r["load"]["before"] for r in runs), "after": max(r["load"]["after"] for r in runs)},
        "shards": shard_rows,
        "merged": {"count": count, "plan_digest": recs[0]["plan_digest"], "merged_shards": [s["index"] for s in shard_rows]},
    }
    mem = {}
    for r in runs:
        mem.update(r.get("memory_processes") or {})
    if mem:
        run["memory_processes"] = mem
    result = {
        "format": results.FORMAT, "run": run,
        "harnesses": dict(sorted(langs.items())),
        "workloads": [wls[w] for w in sorted(wls)],
        "cells": cells,
    }
    results.validate(result)
    return result


def _merged_publishability(parts: list[dict], left_out: dict) -> dict:
    """The rule applied to every shard again from what it measured, plus the reasons each shard
    found that the numbers cannot show (a cell that crashed, a stale deviation list, ...).

    A shard is a narrowed run by construction, so what it says was left out is dropped; the merged
    result gets its own account, from what the shards together leave out.
    """
    rule = results.PUBLISH_RULE
    by_reason: dict[str, list[int]] = {}
    measured = []
    for p in parts:
        idx = p["run"]["shard"]["index"]
        pub = p["run"]["publishability"]
        m = pub["measured"]
        measured.append(m)
        again = results.assess_measured(p["run"]["mode"], m["foreign_busy_cores_p95"], m["foreign_busy_cores_max"],
                                        m["load_samples"], m["load1_start"], m["load1_end"],
                                        m["pinned_cpu"], m["governor"])
        for reason in list(pub["reasons"]) + again:
            if reason.startswith(results.LEFT_OUT_PREFIX):
                continue
            by_reason.setdefault(reason, [])
            if idx not in by_reason[reason]:
                by_reason[reason].append(idx)
    reasons = results.left_out_reasons(left_out)
    n = len(parts)
    for reason, idxs in by_reason.items():
        who = "every shard" if len(idxs) == n else "shard " + ", ".join(map(str, sorted(idxs)))
        reasons.append(reason if n == 1 else f"{who}: {reason}")
    worst = lambda k: max(m[k] for m in measured)  # noqa: E731
    pins = {m["pinned_cpu"] for m in measured}
    govs = {m["governor"] for m in measured}
    return {
        "publishable": not reasons, "label": "publishable" if not reasons else "smoke", "reasons": reasons,
        "rule": dict(rule),
        "measured": {"foreign_busy_cores_p95": worst("foreign_busy_cores_p95"),
                     "foreign_busy_cores_max": worst("foreign_busy_cores_max"),
                     "load1_start": worst("load1_start"), "load1_end": worst("load1_end"),
                     "load_samples": min(m["load_samples"] for m in measured),
                     "pinned_cpu": None if None in pins else min(pins),
                     "governor": govs.pop() if len(govs) == 1 else "mixed"},
    }
