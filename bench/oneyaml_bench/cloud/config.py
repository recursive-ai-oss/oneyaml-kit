"""Account settings for cloud runs.

The settings live in a plain `KEY=value` file outside the repository, named by the
KIT_CLOUD_CONFIG environment variable (default: ~/.config/oneyaml-kit/cloud.env). The file
holds names and identifiers only (region, bucket, base image, limits), never credentials: those
come from the AWS command line tool's usual sources and stay on the local machine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path.home() / ".config" / "oneyaml-kit" / "cloud.env"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    region: str
    bucket: str
    base_ami: str
    instance_type: str = "c7i.metal-24xl"
    cost_cap: float = 25.0
    spot_vcpu_quota: int = 512
    max_shards: int = 16
    volume_gb: int = 40


def parse(text: str) -> dict[str, str]:
    out = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"config line {n}: expected KEY=value")
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def config_path(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("KIT_CLOUD_CONFIG") or DEFAULT_PATH)


def from_values(v: dict[str, str]) -> Config:
    missing = [k for k in ("KIT_CLOUD_REGION", "KIT_CLOUD_BUCKET", "KIT_CLOUD_BASE_AMI") if not v.get(k)]
    if missing:
        raise ConfigError("the cloud config lacks " + ", ".join(missing))
    return Config(
        region=v["KIT_CLOUD_REGION"], bucket=v["KIT_CLOUD_BUCKET"], base_ami=v["KIT_CLOUD_BASE_AMI"],
        instance_type=v.get("KIT_CLOUD_INSTANCE_TYPE", Config.instance_type),
        cost_cap=float(v.get("KIT_CLOUD_COST_CAP", Config.cost_cap)),
        spot_vcpu_quota=int(v.get("KIT_CLOUD_SPOT_VCPU_QUOTA", Config.spot_vcpu_quota)),
        max_shards=int(v.get("KIT_CLOUD_MAX_SHARDS", Config.max_shards)),
        volume_gb=int(v.get("KIT_CLOUD_VOLUME_GB", Config.volume_gb)),
    )


def load(env=None) -> Config:
    p = config_path(env)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read the cloud config {p}: {e.strerror}") from e
    return from_values(parse(text))
