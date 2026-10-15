"""Turns the harness's shard plan into instance counts, lifetimes and costs.

`bin/bench plan --shards N --mode M` prints JSON with a digest and one entry per shard: its
index, its groups (a language on a workload, whole comparison rows) and `estimated_cost`, a
*relative* figure ("(input bytes + fixed share) x tasks x passes"), not seconds. This module
turns it into seconds with a calibration measured on the instances (see `SECONDS_PER_COST`),
and reads only those fields, so the harness may add others.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Seconds from the launch request to the first measured cell on the baked image: 39 to 40 s on c7i.metal-24xl
# (19 s to the kernel, 12 s to the boot script, 3 s setup, 4 s cache warm-up, about 1 s build), in five shards.
BOOT_TO_MEASURING_SECONDS = 40
# Each language a shard measures builds its harness first. Go was measured at 1 to 14 s on the baked image; the
# others are an allowance, not a measurement.
BUILD_SECONDS_PER_LANGUAGE = 30
# Seconds of measuring per unit of the plan's relative cost, by mode. Quick is measured: two Go shards of
# 70.7 and 70.2 million units took 59.8 and 76.4 s (twice, on c7i.metal-24xl). That is about 0.33 s per timed
# cell, nearly all of it fixed cost (guard, preparation, memory pass; the timing itself is about 7 ms). Full is
# derived, not measured: a full cell adds a 300 ms warm-up and 31 samples of about 50 ms (about 1.85 s), so
# about 2.4 s per cell, and the full Go selection has 1.31 times the timed cells of the quick one for 4.9 times
# the cost units. Refit it after the first full run.
SECONDS_PER_COST = {"quick": 9.7e-7, "full": 2.0e-6}
# Instances are billed per second with a one minute minimum.
MIN_BILLED_SECONDS = 60
QUICK_LIFETIME_MINUTES = 45
# The conformance job is not timed. Per library: Go took 13 s to run its 1235 cases plus a few seconds to build,
# so 120 s is an allowance that also covers the Rust and C# builds, which were not measured.
CONFORMANCE_SECONDS_PER_LIBRARY = 120
CONFORMANCE_LIFETIME_MINUTES = 40


class PlanError(Exception):
    pass


@dataclass
class ShardPlan:
    index: int            # 1-based
    seconds: float        # estimated time from the first measured cell to the last
    groups: list


@dataclass
class Plan:
    digest: str
    shards: list[ShardPlan]
    mode: str = "full"

    @property
    def count(self) -> int:
        return len(self.shards)


def startup_seconds(groups: list) -> int:
    """Boot, setup and builds before the first measurement of a shard, for the languages it holds."""
    langs = {g.split("/", 1)[0] for g in groups if isinstance(g, str)}
    return BOOT_TO_MEASURING_SECONDS + BUILD_SECONDS_PER_LANGUAGE * max(len(langs), 1)


def parse_plan(obj, mode: str = "full") -> Plan:
    """Reads the planner's JSON. Raises PlanError on a shape it cannot use."""
    if not isinstance(obj, dict):
        raise PlanError("the plan is not a JSON object")
    digest = obj.get("plan_digest")
    if not isinstance(digest, str) or not digest:
        raise PlanError("the plan has no plan_digest")
    raw = obj.get("shards")
    if not isinstance(raw, list) or not raw:
        raise PlanError("the plan lists no shards")
    k = SECONDS_PER_COST[mode]
    shards = []
    for i, s in enumerate(raw, 1):
        if not isinstance(s, dict) or not isinstance(s.get("estimated_cost"), (int, float)) or "index" not in s:
            raise PlanError(f"shard {i} of the plan has no index or estimated_cost")
        shards.append(ShardPlan(int(s["index"]), float(s["estimated_cost"]) * k, list(s.get("groups", []))))
    if obj.get("count") not in (None, len(shards)):
        raise PlanError(f"the plan says {obj.get('count')} shards and lists {len(shards)}")
    return Plan(digest, shards, mode)


def run_planner(kit_root: Path, n: int, mode: str, filters: list[str]) -> Plan:
    """`filters` are the arguments that narrow the run (--lang, --task, ...), the same ones the shards get."""
    cmd = [sys.executable, str(kit_root / "bin" / "bench"), "plan", "--shards", str(n), "--mode", mode, *filters]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise PlanError(f"bin/bench plan failed: {r.stderr.strip()[-300:]}")
    try:
        return parse_plan(json.loads(r.stdout), mode)
    except ValueError as e:
        raise PlanError(f"bin/bench plan printed no JSON: {e}") from e


def lifetime_minutes(shard: ShardPlan, mode: str) -> int:
    """The hard limit of an instance: the estimate with generous slack, never less than a floor."""
    if mode == "quick":
        return QUICK_LIFETIME_MINUTES
    return int(math.ceil((startup_seconds(shard.groups) + shard.seconds * 1.5) / 60)) + 15


def instance_seconds(shard: ShardPlan) -> float:
    return startup_seconds(shard.groups) + shard.seconds


def shard_cost(price: float, seconds: float) -> float:
    return price * max(MIN_BILLED_SECONDS, seconds) / 3600.0


def conformance_instance_seconds(libraries: int) -> float:
    return BOOT_TO_MEASURING_SECONDS + CONFORMANCE_SECONDS_PER_LIBRARY * libraries if libraries else 0.0


def plan_cost(plan: Plan, price: float, conformance: int = 0) -> float:
    """Estimated cost of the shards plus a conformance job for `conformance` libraries (0: none)."""
    total = sum(shard_cost(price, instance_seconds(s)) for s in plan.shards)
    return total + (shard_cost(price, conformance_instance_seconds(conformance)) if conformance else 0.0)


def wall_seconds(plan: Plan, conformance: int = 0) -> float:
    return max(max(instance_seconds(s) for s in plan.shards), conformance_instance_seconds(conformance))


def choose_shards(get_plan, max_n: int, price: float, cap: float, conformance: int = 0) -> tuple[int, Plan, list[dict]]:
    """The shard count with the shortest wall-clock time whose estimated cost stays under the cap.

    `get_plan(n)` returns the plan for n shards, or raises PlanError when the harness cannot split
    that far. Returns the count, its plan and the table of candidates, which the run prints."""
    table, best = [], None
    for n in range(1, max_n + 1):
        try:
            plan = get_plan(n)
        except PlanError:
            if n == 1:
                raise
            break
        row = {"shards": plan.count, "wall_seconds": round(wall_seconds(plan, conformance)),
               "cost": round(plan_cost(plan, price, conformance), 2)}
        table.append(row)
        if row["cost"] > cap:
            continue
        if best is None or wall_seconds(plan, conformance) < wall_seconds(best[1], conformance) * 0.97:
            best = (n, plan)
    if best is None:
        raise PlanError(f"no shard count fits under the cost cap of ${cap:.2f}; the cheapest estimate is "
                        f"${min(r['cost'] for r in table):.2f}")
    return best[0], best[1], table
