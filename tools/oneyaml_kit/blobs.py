"""Repository hygiene: no executables, core dumps or large blobs in what is tracked or in history.

A file counts when it is in the index (tracked or staged), when it is new and not ignored (read from the
working tree, because it is about to be added), or when it is in a commit that git can reach from the revisions
given. History matters because a blob added in one commit and deleted in the next is gone from the tip but still
ships with the history. It does not look for secrets.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path

LIMIT = 1_048_576  # a blob larger than this many bytes is refused; exactly this many is allowed

# Leading bytes of native executables and dump files. 0xCAFEBABE is left out: Java class files share it.
MAGIC = {
    b"\x7fELF": "an ELF file (executable, library or core dump)",
    b"\xfe\xed\xfa\xce": "a Mach-O binary", b"\xfe\xed\xfa\xcf": "a Mach-O binary",
    b"\xce\xfa\xed\xfe": "a Mach-O binary", b"\xcf\xfa\xed\xfe": "a Mach-O binary",
    b"MDMP": "a Windows minidump",
}
# File names (the last path component) of dumps and profiler output. The first four are the names the
# top-level ignore file already bans.
# systemd-coredump writes compressed cores as core.<command>.<uid>.<boot>.<pid>.<time>.zst (or xz, lz4).
DUMP_NAMES = ["core", "core.[0-9]*", "core.*.zst", "core.*.xz", "core.*.lz4", "vgcore.*", "*.core",
              "perf.data", "perf.data.*", "*.hprof", "*.mdmp"]
UNNAMED = "(no path: reachable without a tree)"
ZERO = "0" * 40


class GitError(Exception):
    pass


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    r = subprocess.run(["git", *args], cwd=root, input=data, capture_output=True)
    if r.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout


def name_reason(path: str) -> str | None:
    base = path.rsplit("/", 1)[-1]
    for pat in DUMP_NAMES:
        if fnmatch.fnmatchcase(base, pat):
            return f"named like a core dump or profiler output ({pat})"
    return None


def inspect(entries: dict[str, list[str]], root: Path) -> list[str]:
    """Problems for blobs: `entries` maps a blob id to the paths it was seen under."""
    if not entries:
        return []
    ids = "".join(i + "\n" for i in entries).encode()
    sizes: dict[str, int] = {}
    fmt = "--batch-check=%(objectname) %(objecttype) %(objectsize)"
    for line in git(root, "cat-file", fmt, data=ids).decode().splitlines():
        oid, kind, size = line.split()
        if kind == "blob":
            sizes[oid] = int(size)
    problems = []
    small = [i for i, n in sizes.items() if n <= LIMIT]
    # Only the first bytes of a blob matter, but cat-file hands over all of it; blobs this small are cheap.
    heads = _heads(root, small)
    for oid, size in sorted(sizes.items(), key=lambda kv: entries[kv[0]][0]):
        # One blob can sit under several names; each name is reported, with what the content says.
        for path in entries[oid]:
            for w in reasons(path, size, heads.get(oid, b"")):
                problems.append(f"{path}: {w} [{oid[:12]}]")
    return problems


def reasons(path: str, size: int, head: bytes) -> list[str]:
    """Why a file of this name, size and first bytes is refused; empty when it is fine."""
    why = []
    if size > LIMIT:
        why.append(f"{size} bytes, over the limit of {LIMIT}")
    magic = MAGIC.get(head[:4])
    if magic:
        why.append(magic)
    named = name_reason(path)
    if named:
        why.append(named)
    return why


def check_untracked(root: Path) -> list[str]:
    """New files that are not ignored, read from disk: a hand-over leaves them untracked, and the next `git add`
    would take them in."""
    problems = []
    listing = git(root, "ls-files", "--others", "--exclude-standard", "-z").decode("utf-8", "surrogateescape")
    for rel in listing.split("\0"):
        path = root / rel
        if not rel or path.is_symlink() or not path.is_file():
            continue
        with open(path, "rb") as f:
            head = f.read(4)
        for w in reasons(rel, os.path.getsize(path), head):
            problems.append(f"{rel}: {w} [untracked]")
    return problems


def _heads(root: Path, ids: list[str]) -> dict[str, bytes]:
    out = git(root, "cat-file", "--batch", data="".join(i + "\n" for i in ids).encode())
    heads, pos = {}, 0
    while pos < len(out):
        end = out.index(b"\n", pos)
        oid, _kind, size = out[pos:end].decode().split()
        start = end + 1
        heads[oid] = out[start:start + 4]
        pos = start + int(size) + 1
    return heads


def check_index(root: Path) -> list[str]:
    """The files in the index: tracked, and staged for the next commit."""
    entries: dict[str, list[str]] = {}
    for rec in git(root, "ls-files", "-s", "-z").decode("utf-8", "surrogateescape").split("\0"):
        if not rec:
            continue
        meta, path = rec.split("\t", 1)
        mode, oid, _stage = meta.split()
        if mode != "160000":  # a submodule is a commit, not a blob
            entries.setdefault(oid, []).append(path)
    return inspect(entries, root)


def check_history(root: Path, *revs: str) -> list[str]:
    """Every blob in the commits that `git rev-list` takes for `revs` (a range such as A..B, or one revision for
    its whole history), including blobs a later commit deleted."""
    if git(root, "rev-parse", "--is-shallow-repository").strip() == b"true":
        raise GitError("this clone is shallow, so its history is incomplete; fetch the full history first")
    entries: dict[str, list[str]] = {}
    for line in git(root, "rev-list", "--objects", *revs).decode("utf-8", "surrogateescape").splitlines():
        oid, _, path = line.partition(" ")
        # An object with no path, such as a blob a tag points at directly, is still checked.
        entries.setdefault(oid, []).append(path or UNNAMED)
    return inspect(entries, root)


def check_push(root: Path, before: str, after: str) -> list[str]:
    """The commits a push added. A new branch (no previous tip) or a previous tip this clone does not have, as
    after a forced push, cannot give a range, so the whole history of the new tip is checked."""
    if not re.fullmatch(r"[0-9a-f]{40}", after):
        raise GitError(f"not a commit id: {after!r}")
    known = before != ZERO and re.fullmatch(r"[0-9a-f]{40}", before) and _has(root, before)
    return check_history(root, f"{before}..{after}") if known else check_history(root, after)


def _has(root: Path, oid: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", oid + "^{commit}"], cwd=root, capture_output=True).returncode == 0
