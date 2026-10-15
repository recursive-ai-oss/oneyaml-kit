"""Finds and removes everything a run (or a crashed launcher) left behind.

Every resource a run creates carries the run's tag, so nothing needs remembering: the cleanup
asks the account what is tagged, terminates and deletes it, and then asks again to prove that
nothing is left.
"""

from __future__ import annotations

import time

from .runner import Teardown

# The bucket's lifecycle rule expires objects after a day; the cleanup removes older leftovers sooner than that.
S3_MAX_AGE_SECONDS = 24 * 3600


def inventory(aws, run_id: str | None = None, s3_older_than: float | None = None) -> dict[str, list[str]]:
    """Every tagged resource that still exists (instances that are terminated do not count)."""
    return {
        "instances": [f"{i.id} {i.state} {i.type} {i.lifecycle}" for i in aws.instances(run_id) if i.state != "terminated"],
        "spot_requests": aws.spot_requests(run_id),
        "security_groups": aws.security_groups(run_id),
        "launch_templates": aws.launch_templates(run_id),
        "volumes": aws.volumes(run_id),
        "s3_objects": aws.list_keys(f"runs/{run_id}/" if run_id else "runs/", older_than=s3_older_than),
    }


def still_running(inv: dict[str, list[str]]) -> list[str]:
    """Instances that are not on their way out. One that is shutting down is no longer billed."""
    return [i for i in inv["instances"] if i.split()[1] != "shutting-down"]


def total(inv: dict[str, list[str]]) -> int:
    return sum(len(v) for v in inv.values())


def cleanup(aws, run_id: str | None = None, say=print, now=time.time, wait: float = 900.0) -> dict[str, list[str]]:
    """Terminates and deletes tagged leftovers of one run, or of all runs, and returns what the account still holds.
    `wait` bounds how long it waits for instances to finish terminating (bare metal takes minutes).

    Without a run id, stored objects are removed only once they are older than the bucket's expiry, so a run in
    progress keeps its inputs."""
    old = None if run_id else now() - S3_MAX_AGE_SECONDS
    before = inventory(aws, run_id, old)
    say(f"tagged resources found: {total(before)}")
    Teardown(aws, run_id, say).run(quiet=True, wait=wait)
    aws.delete_keys(before["s3_objects"])
    after = inventory(aws, run_id, old)
    for k, v in after.items():
        say(f"  {k}: {len(v)}")
    say(f"tagged resources left: {total(after)}")
    return after
