"""Turns the harness's shard plan into instance counts, lifetimes and costs.

`bin/bench plan --shards N` prints JSON with one entry per shard: its groups of whole
comparison rows and an estimate of its run time in seconds, plus a digest of the plan. This
module reads only those fields, so the harness may add others.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Seconds an instance spends before its first cell is measured (boot, setup, input download,
# build). The figure is a measured default; a run reports the real one.
BOOT_TO_MEASURING_SECONDS = 240
# Instances are billed per second with a one minute minimum.
MIN_BILLED_SECONDS = 60
QUICK_LIFETIME_MINUTES = 45


class PlanError(Exception):
    pass


@dataclass
class ShardPlan:
    index: int            # 1-based
    seconds: float        # estimated measuring time
    groups: list


@dataclass
class Plan:
    digest: str
    shards: list[ShardPlan]

    @property
    def count(self) -> int:
        return len(self.shards)


def parse_plan(obj, expected: int | None = None) -> Plan:
    """Reads the planner's JSON. Raises PlanError on a shape it cannot use."""
    if not isinstance(obj, dict):
        raise PlanError("the plan is not a JSON object")
    digest = obj.get("plan_digest")
    if not isinstance(digest, str) or not digest:
        raise PlanError("the plan has no plan_digest")
    raw = obj.get("shards")
    if isinstance(raw, dict):
        raw = [dict(v, shard=int(k)) if isinstance(v, dict) else v for k, v in sorted(raw.items(), key=lambda kv: int(kv[0]))]
    if not isinstance(raw, list) or not raw:
        raise PlanError("the plan lists no shards")
    shards = []
    for i, s in enumerate(raw, 1):
        if not isinstance(s, dict):
            raise PlanError(f"shard {i} of the plan is not an object")
        secs = next((s[k] for k in ("estimated_seconds", "seconds", "estimated_cost", "cost") if isinstance(s.get(k), (int, float))), None)
        if secs is None:
            raise PlanError(f"shard {i} of the plan has no time estimate")
        shards.append(ShardPlan(int(s.get("shard", i)), float(secs), s.get("groups", [])))
    if expected is not None and len(shards) != expected:
        raise PlanError(f"asked for {expected} shards, the plan has {len(shards)}")
    return Plan(digest, shards)


def run_planner(kit_root: Path, n: int, langs: list[str] | None) -> Plan:
    cmd = [sys.executable, str(kit_root / "bin" / "bench"), "plan", "--shards", str(n)]
    for lang in langs or []:
        cmd += ["--lang", lang]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise PlanError(f"bin/bench plan failed: {r.stderr.strip()[-300:]}")
    try:
        return parse_plan(json.loads(r.stdout), None)
    except ValueError as e:
        raise PlanError(f"bin/bench plan printed no JSON: {e}") from e


def lifetime_minutes(shard_seconds: float, mode: str) -> int:
    """The hard limit of an instance: the estimate with generous slack, never less than a floor."""
    if mode == "quick":
        return QUICK_LIFETIME_MINUTES
    return int(math.ceil((BOOT_TO_MEASURING_SECONDS + shard_seconds * 1.5) / 60)) + 15


def shard_cost(price: float, shard_seconds: float) -> float:
    billed = max(MIN_BILLED_SECONDS, BOOT_TO_MEASURING_SECONDS + shard_seconds)
    return price * billed / 3600.0


def plan_cost(plan: Plan, price: float) -> float:
    return sum(shard_cost(price, s.seconds) for s in plan.shards)


def wall_seconds(plan: Plan) -> float:
    return BOOT_TO_MEASURING_SECONDS + max(s.seconds for s in plan.shards)


def choose_shards(get_plan, max_n: int, price: float, cap: float) -> tuple[int, Plan, list[dict]]:
    """The shard count with the shortest wall-clock time whose estimated cost stays under the cap.

    `get_plan(n)` returns the plan for n shards. Returns the count, its plan and the table
    of candidates, which the run prints so the choice can be checked."""
    table, best = [], None
    for n in range(1, max_n + 1):
        plan = get_plan(n)
        row = {"shards": plan.count, "wall_seconds": round(wall_seconds(plan)), "cost": round(plan_cost(plan, price), 2)}
        table.append(row)
        if row["cost"] > cap:
            continue
        if best is None or wall_seconds(plan) < wall_seconds(best[1]) * 0.97:
            best = (n, plan)
        if plan.count < n:      # the harness cannot split further
            break
    if best is None:
        raise PlanError(f"no shard count fits under the cost cap of ${cap:.2f}; the cheapest estimate is ${min(r['cost'] for r in table):.2f}")
    return best[0], best[1], table
