"""Bakes (or reuses) the machine image every run starts from.

The image holds the Go, .NET and Rust toolchains, the system packages, and warm module, package
and build caches for the three harnesses, so an instance can start measuring a short while after
it boots. It is looked up by the digest of what it contains (toolchains.digest); a new image is
built only when that digest changes.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from . import bootstrap, pack as packmod, toolchains
from .aws import Launch, http_get
from .runner import RunFailed, Teardown, ensure_signal_handlers, shield_interrupts

BAKE_TYPE = "c7i.4xlarge"
BAKE_MINUTES = 40


def current_digest(cfg, kit_root: Path) -> str:
    return toolchains.digest(kit_root, cfg.base_ami, bootstrap.bake_script("-", "-", "-", 0, "-"))


def ensure_image(aws, cfg, kit_root: Path, work: Path, say=print, force: bool = False, poll: float = 10.0) -> tuple[str, dict]:
    """The image id for the current digest and what was done to get it."""
    digest = current_digest(cfg, kit_root)
    if not force:
        found = aws.find_image(digest)
        if found:
            say(f"image {found} matches digest {digest}; reusing it")
            return found, {"digest": digest, "baked": False}
    say(f"no image for digest {digest}; baking one")
    t0 = time.time()
    info = bake(aws, cfg, kit_root, work, digest, say, poll)
    info.update(digest=digest, baked=True, seconds=round(time.time() - t0))
    return info["image"], info


def bake(aws, cfg, kit_root: Path, work: Path, digest: str, say, poll: float) -> dict:
    """Bakes on an on-demand instance that stops (rather than terminates) when its script ends, so the volume can
    be imaged. The instance is terminated when the bake ends, whether it worked, failed or was interrupted: the
    `finally` below runs on every way out, SIGTERM and SIGHUP included."""
    ensure_signal_handlers()
    run_id = "bake" + time.strftime("%m%d%H%M%S") + uuid.uuid4().hex[:3]
    td = Teardown(aws, run_id, say)
    deadline = time.time() + (BAKE_MINUTES + 45) * 60
    image_id = None
    try:
        work.mkdir(parents=True, exist_ok=True)
        packs = [packmod.archive_git("kit", kit_root, work / "kit-bake.tar.gz", packmod.KIT_PATHS)]
        bundle = packmod.bundle_inputs(packs, work / "bake-inputs.tar")
        sg = aws.create_security_group(run_id, deadline)
        td.security_groups.append(sg)
        pre = aws.presigner(BAKE_MINUTES * 60 + 1800)
        aws.upload(bundle, f"runs/{run_id}/inputs.tar")
        base = f"runs/{run_id}/bake"
        script = bootstrap.bake_script(pre.url("GET", f"runs/{run_id}/inputs.tar"), pre.url("PUT", base + "/log.txt"),
                                       pre.url("PUT", base + "/status.json"), BAKE_MINUTES, run_id)
        iid = aws.launch(Launch(image=cfg.base_ami, type=BAKE_TYPE, security_group=sg, user_data=bootstrap.pack_user_data(script),
                                run_id=run_id, name=f"oneyaml-kit-{run_id}", spot=False, volume_gb=cfg.volume_gb,
                                stop_on_shutdown=True, deadline=deadline))
        say(f"bake instance {iid} started")
        shown = 0
        status = None
        end = time.time() + (BAKE_MINUTES + 5) * 60
        while time.time() < end:
            log = http_get(pre.url("GET", base + "/log.txt"))
            if log:
                text = log.decode("utf-8", "replace")
                for line in text[shown:].splitlines()[-40:]:
                    say(f"[bake] {line}")
                shown = len(text)
            raw = http_get(pre.url("GET", base + "/status.json"))
            if raw:
                status = json.loads(raw)
                break
            time.sleep(poll)
        if not status or status.get("exit") != 0:
            raise RunFailed(f"the image bake failed: {status or 'no status before the deadline'}")
        # The instance powers itself off after uploading its status; wait for the stop, then image the volume.
        while time.time() < end:
            inst = next((i for i in aws.instances(run_id) if i.id == iid), None)
            if inst and inst.state == "stopped":
                break
            time.sleep(poll)
        else:
            raise RunFailed("the bake instance did not stop")
        image_id = aws.create_image(iid, digest, f"oneyaml-kit-bench-{digest}")
        say(f"creating image {image_id}")
        aws.wait_image(image_id)
        return {"image": image_id}
    finally:
        shield_interrupts()
        td.run()

