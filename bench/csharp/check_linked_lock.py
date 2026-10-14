#!/usr/bin/env python3
"""Checks a linked build's restore against the committed lock file.

A build that links a library checkout cannot restore in locked mode (the checkout is a project
reference, which a lock file cannot hold), so NuGet writes a second lock file for it. The packages
the competitors bring must still resolve exactly as in packages.lock.json: otherwise the rows that
compare our library with them would be measured against different code than the published ones.
The checkout may add packages of its own; it may not change or drop any that are locked.

Usage: check_linked_lock.py PACKAGES_LOCK LINKED_LOCK. Exit 0 when they agree, 1 with the
differences on stderr otherwise (a missing or unreadable file counts as a difference).
"""
import json
import sys


def resolved(path):
    """{(framework, package): resolved version} for every non-project dependency."""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    out = {}
    for framework, deps in doc.get("dependencies", {}).items():
        for name, info in deps.items():
            if info.get("type") != "Project":
                out[(framework, name)] = info.get("resolved")
    return out


def differences(locked, linked):
    problems = []
    for key, version in sorted(locked.items()):
        framework, name = key
        if key not in linked:
            problems.append(f"{name} ({framework}): locked at {version}, missing from the linked restore")
        elif linked[key] != version:
            problems.append(f"{name} ({framework}): locked at {version}, the linked restore resolves {linked[key]}")
    return problems


def main(argv):
    if len(argv) != 3:
        print("usage: check_linked_lock.py PACKAGES_LOCK LINKED_LOCK", file=sys.stderr)
        return 2
    try:
        problems = differences(resolved(argv[1]), resolved(argv[2]))
    except (OSError, ValueError) as e:
        problems = [f"cannot read a lock file: {e}"]
    for p in problems:
        print("linked build: " + p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
