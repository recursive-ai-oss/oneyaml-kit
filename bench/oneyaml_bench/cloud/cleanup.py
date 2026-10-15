"""Finds and removes everything a run (or a crashed launcher) left behind.

Every resource a run creates carries the run's tag and a deadline tag, so nothing needs remembering: the
cleanup asks the account what is tagged, terminates and deletes it, and then asks again to prove that nothing
is left.

Asked for one run (`--run`), it removes that run's resources whatever their deadline. Asked for everything, it
removes only the runs whose deadline has passed: another launcher may be running right now, and its instances
are not leftovers.
"""

from __future__ import annotations

import time

from .runner import Teardown

# The bucket's lifecycle rule expires objects after a day; the cleanup removes older leftovers sooner than that.
S3_MAX_AGE_SECONDS = 24 * 3600


def inventory(aws, run_id: str | None = None, s3_older_than: float | None = None) -> dict[str, list[str]]:
    """Every tagged resource of the run (of every run when None) that still exists. Terminated instances do not count."""
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
    return sum(len(v) for k, v in inv.items() if k != "left_alone")


def merge_inventories(parts: list[dict[str, list[str]]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in parts:
        for k, v in p.items():
            out.setdefault(k, []).extend(v)
    return out


def expired_runs(aws, now: float) -> tuple[list[str], dict[str, float]]:
    """The runs whose deadline has passed, and the runs still inside theirs with that deadline."""
    deadlines = aws.run_deadlines()
    return (sorted(r for r, d in deadlines.items() if d < now),
            {r: d for r, d in deadlines.items() if d >= now})


def gather(aws, runs: list[str], young: dict[str, float], orphan_age: float | None) -> dict[str, list[str]]:
    """What the given runs hold. With `orphan_age`, stored objects of unknown runs older than that count too: a
    run that deleted its own resources but not its inputs leaves nothing else to find it by."""
    inv = merge_inventories([inventory(aws, r) for r in runs])
    for k in ("instances", "spot_requests", "security_groups", "launch_templates", "volumes", "s3_objects"):
        inv.setdefault(k, [])
    if orphan_age is not None:
        known = set(runs) | set(young)
        inv["s3_objects"] += [k for k in aws.list_keys("runs/", older_than=orphan_age)
                              if k.split("/")[1] not in known and k not in inv["s3_objects"]]
    return inv


def cleanup(aws, run_id: str | None = None, say=print, now=time.time, wait: float = 900.0,
            keep_image_digest: str | None = None) -> dict[str, list[str]]:
    """Terminates and deletes tagged leftovers of one run, or of every run past its deadline, and returns what
    the account still holds. `wait` bounds how long it waits for instances to finish terminating (bare metal
    takes minutes).

    Without a run id, runs inside their deadline are left alone and stored objects of unknown runs are removed
    once they are older than the bucket's expiry. With `keep_image_digest`, machine images of another digest
    are removed too; the current one stays."""
    if run_id:
        runs, young, orphan_age = [run_id], {}, None
    else:
        runs, young = expired_runs(aws, now())
        orphan_age = now() - S3_MAX_AGE_SECONDS
    before = gather(aws, runs, young, orphan_age)
    say(f"tagged resources found: {total(before)}")
    for r, d in sorted(young.items()):
        say(f"left alone: run {r} is inside its deadline ({int(d - now())} s left); `--run {r}` removes it anyway")
    for r in runs:
        Teardown(aws, r, say).run(quiet=True, wait=wait)
    aws.delete_keys(before["s3_objects"])
    removed = remove_superseded_images(aws, keep_image_digest, say) if keep_image_digest and not run_id else []
    after = gather(aws, runs, young, orphan_age)
    for k, v in after.items():
        say(f"  {k}: {len(v)}")
    if removed:
        say(f"  superseded images removed: {', '.join(removed)}")
    if young:
        after["left_alone"] = sorted(young)
    say(f"tagged resources left: {total(after)}")
    return after


def _images_in_use(aws) -> set[str]:
    return {i.image for i in aws.instances(None) if i.state != "terminated" and i.image}


def remove_superseded_images(aws, keep_digest: str, say=print) -> list[str]:
    """Deregisters the images of an older digest and deletes their snapshots, which are billed by the month."""
    gone = []
    for img in aws.superseded_images(keep_digest, _images_in_use(aws)):
        aws.remove_image(img["image"], img["snapshots"])
        gone.append(img["image"])
    return gone
