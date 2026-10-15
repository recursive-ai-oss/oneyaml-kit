"""Packs the kit and the library sources into tarballs for the instances.

A tarball is a `git archive` of HEAD, so what runs on the instance is exactly a commit and the
revision can be recorded. Uncommitted work is left out and reported as `dirty`. The kit alone
can instead be packed from its working tree (`--kit-tree`), for trying a change that is not
committed yet; such a run records the kit as modified.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

# The parts of the kit a run needs: the benchmark harnesses, and for the conformance run the case suites and adapters.
KIT_PATHS = ("bin", "bench", "tools", "suites", "adapters")
SKIP_PARTS = {".git", "__pycache__", "out", "target", "bin-go", "node_modules"}

# Library sources the harnesses can link in, by the variable that names their checkout.
LIBRARY_VARS = {"KIT_ONEYAML_GO_SRC": "oneyaml-go", "KIT_ONEYAML_CSHARP_SRC": "oneyaml-csharp"}


class PackError(Exception):
    pass


@dataclass
class Packed:
    name: str            # kit, oneyaml-go, ...
    path: Path           # the tarball
    revision: str        # full commit id the content came from
    dirty: bool          # uncommitted changes exist in the source (not in a git archive)
    source: str          # "git" or "tree"
    sha256: str
    size: int
    env_var: str | None = None


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise PackError(f"git {' '.join(args[:2])} failed in {repo.name}: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def archive_git(name: str, repo: Path, dest: Path, paths: tuple[str, ...] = (), env_var: str | None = None) -> Packed:
    """`git archive` of HEAD (limited to `paths` when given) as a gzip tarball."""
    repo = repo.resolve()
    rev = _git(repo, "rev-parse", "HEAD")
    dirty = bool(_git(repo, "status", "--porcelain", "--", "."))
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        r = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar.gz", "-6", "HEAD", "--", *(paths or (".",))],
                           stdout=f, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise PackError(f"git archive failed in {name}: {r.stderr.decode(errors='replace').strip()[-300:]}")
    return Packed(name, dest, rev, dirty, "git", _digest(dest), dest.stat().st_size, env_var)


def archive_tree(name: str, root: Path, dest: Path, paths: tuple[str, ...]) -> Packed:
    """A tarball of the working tree, for a kit change that is not committed yet."""
    root = root.resolve()
    try:
        rev = _git(root, "rev-parse", "HEAD")
    except PackError:
        rev = "0" * 40
    dest.parent.mkdir(parents=True, exist_ok=True)

    def keep(info: tarfile.TarInfo):
        if SKIP_PARTS & set(Path(info.name).parts):
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    # Tracked and untracked files that git does not ignore: the same set a commit of the working tree would
    # hold, so a stray build directory or cache is left out the way the repository's ignore rules say.
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "-co", "--exclude-standard", "--", *paths],
                            capture_output=True)
    with tarfile.open(dest, "w:gz", compresslevel=6) as tf:
        if listed.returncode == 0:
            for rel in sorted(set(listed.stdout.decode("utf-8", "surrogateescape").split("\0")) - {""}):
                # A file deleted in the working tree is still listed as tracked.
                if (root / rel).is_file() or (root / rel).is_symlink():
                    tf.add(root / rel, arcname=rel, recursive=False, filter=keep)
        else:       # not a git checkout: walk the named parts
            for p in paths:
                if (root / p).exists():
                    tf.add(root / p, arcname=p, filter=keep)
    return Packed(name, dest, rev, True, "tree", _digest(dest), dest.stat().st_size)


def pack_all(kit_root: Path, dest_dir: Path, kit_tree: bool = False, env=None) -> list[Packed]:
    """The kit plus every library source the environment names."""
    env = os.environ if env is None else env
    out = []
    if kit_tree:
        out.append(archive_tree("kit", kit_root, dest_dir / "kit.tar.gz", KIT_PATHS))
    else:
        out.append(archive_git("kit", kit_root, dest_dir / "kit.tar.gz", KIT_PATHS))
    for var, name in LIBRARY_VARS.items():
        src = env.get(var)
        if src:
            if not Path(src).is_dir():
                raise PackError(f"{var} names {src}, which is not a directory")
            out.append(archive_git(name, Path(src), dest_dir / f"{name}.tar.gz", env_var=var))
    return out


def describe(packs: list[Packed]) -> list[dict]:
    """What a run records about its inputs. Paths stay out: only names, revisions and digests."""
    return [{"name": p.name, "revision": p.revision, "dirty": p.dirty, "source": p.source, "sha256": p.sha256,
             "bytes": p.size} for p in packs]


def bundle_inputs(packs: list[Packed], path: Path) -> Path:
    """One outer tar with the kit, the libraries and their revisions: a single download per instance."""
    with tarfile.open(path, "w") as tf:
        for p in packs:
            tf.add(p.path, arcname="kit.tar.gz" if p.name == "kit" else f"lib-{p.name}.tar.gz")
            # A working-tree pack is not a commit, and the file must not pass it off as one.
            data = (p.revision + ("+modified" if p.source == "tree" else "") + "\n").encode()
            info = tarfile.TarInfo(f"rev-{p.name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
            if p.name == "kit":
                # The revision the launcher hands the run as KIT_REVISION: an archive has no history
                # for the harness to read it from. A working-tree pack is not a commit, so it says so.
                label = (p.revision[:12] + ("+modified" if p.source == "tree" else "") + "\n").encode()
                info = tarfile.TarInfo("kit-label")
                info.size = len(label)
                tf.addfile(info, io.BytesIO(label))
    return path
