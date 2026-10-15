"""The toolchains baked into the instance image, and the digest that decides when to rebuild it.

An image is reused as long as the digest matches: the digest covers the pinned versions, the
base image, the bake script and the dependency lock files of the three harnesses. A change to
any of them yields a new digest and so a new image.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

PINS = {
    "go": "1.26.0",
    "dotnet": "10.0.400",
    "rust": "1.97.1",
    "apt": ["build-essential", "git", "curl", "ca-certificates", "valgrind", "python3", "unzip", "xz-utils"],
}
RUSTUP_URL = "https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init"

# Files whose content decides which packages and modules the image has to hold.
LOCK_FILES = (
    "bench/go/go.mod", "bench/go/go.sum",
    "bench/rust/Cargo.toml", "bench/rust/Cargo.lock",
    "bench/csharp/Bench.csproj", "bench/csharp/packages.lock.json", "bench/csharp/Directory.Build.props",
)


def go_url() -> str:
    return f"https://go.dev/dl/go{PINS['go']}.linux-amd64.tar.gz"


def dotnet_url() -> str:
    v = PINS["dotnet"]
    return f"https://builds.dotnet.microsoft.com/dotnet/Sdk/{v}/dotnet-sdk-{v}-linux-x64.tar.gz"


def digest(kit_root: Path, base_ami: str, bake_script_template: str) -> str:
    """A short hex digest of everything the image content depends on."""
    h = hashlib.sha256()
    h.update(json.dumps(PINS, sort_keys=True).encode())
    h.update(base_ami.encode())
    h.update(bake_script_template.encode())
    for rel in LOCK_FILES:
        p = kit_root / rel
        h.update(rel.encode())
        h.update(p.read_bytes() if p.exists() else b"-")
    return h.hexdigest()[:16]
