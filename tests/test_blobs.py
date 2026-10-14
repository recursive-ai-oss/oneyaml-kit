"""The repository hygiene check: executables, core dumps and large blobs, in the index and in history."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from oneyaml_kit import blobs

ELF = b"\x7fELF\x02\x01\x01" + b"\0" * 57


class Repo:
    """A throw-away repository; commits are made with a fixed identity so no setup is needed."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
        self.git("init", "-q")

    def git(self, *args):
        r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                            "-c", "commit.gpgsign=false", *args], cwd=self.dir, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def write(self, name, data):
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def commit(self, message="c"):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def remove(self):
        shutil.rmtree(self.dir)


class BlobCheck(unittest.TestCase):
    def repo(self):
        r = Repo()
        self.addCleanup(r.remove)
        r.write("README.md", b"text\n")
        return r

    def test_an_elf_blob_fails(self):
        r = self.repo()
        r.write("tools/helper", ELF)
        r.commit()
        out = blobs.check_index(r.dir)
        self.assertEqual(len(out), 1, out)
        self.assertIn("tools/helper", out[0])
        self.assertIn("ELF", out[0])

    def test_dump_names_fail_whatever_they_hold(self):
        r = self.repo()
        for name in ("core.123", "core", "sub/vgcore.77", "x.core", "perf.data", "perf.data.old", "heap.hprof"):
            r.write(name, b"harmless text\n")
        r.commit()
        out = blobs.check_index(r.dir)
        self.assertEqual(len(out), 7, out)
        r.write("core.123", b"changed\n")

    def test_names_that_only_resemble_dumps_pass(self):
        r = self.repo()
        for name in ("core.md", "coreutils", "score", "docs/core/notes.txt", "perf.txt"):
            r.write(name, b"text\n")
        r.commit()
        self.assertEqual(blobs.check_index(r.dir), [])

    def test_a_file_over_one_mebibyte_fails_and_one_of_exactly_that_size_passes(self):
        r = self.repo()
        r.write("big/exact.bin", b"a" * blobs.LIMIT)
        r.commit()
        self.assertEqual(blobs.check_index(r.dir), [])
        r.write("big/over.bin", b"a" * (blobs.LIMIT + 1))
        r.commit()
        out = blobs.check_index(r.dir)
        self.assertEqual(len(out), 1, out)
        self.assertIn("big/over.bin", out[0])
        self.assertIn(str(blobs.LIMIT + 1), out[0])

    def test_a_staged_file_counts_before_it_is_committed(self):
        r = self.repo()
        r.commit()
        r.write("late.bin", ELF)
        self.assertEqual(blobs.check_index(r.dir), [])  # in the working tree only
        r.git("add", "late.bin")
        self.assertEqual(len(blobs.check_index(r.dir)), 1)

    def test_a_blob_added_and_deleted_again_still_fails_the_range_check(self):
        r = self.repo()
        base = r.commit()
        r.write("dump/core.1", ELF)
        r.write("big.dat", b"z" * (blobs.LIMIT + 1))
        added = r.commit("add")
        os.remove(r.dir / "dump" / "core.1")
        os.remove(r.dir / "big.dat")
        tip = r.commit("remove")
        self.assertEqual(blobs.check_index(r.dir), [])  # the tip is clean
        out = blobs.check_history(r.dir, f"{base}..{tip}")
        self.assertEqual(len([o for o in out if "core.1" in o]), 2, out)  # by its name and by its magic
        self.assertTrue(any("big.dat" in o for o in out), out)
        # The range that starts after the offending commit does not see it.
        self.assertEqual(blobs.check_history(r.dir, f"{added}..{tip}"), [])
        # A push is judged by the commits it added.
        self.assertTrue(blobs.check_push(r.dir, base, tip))
        self.assertEqual(blobs.check_push(r.dir, added, tip), [])

    def test_a_new_branch_or_an_unknown_previous_tip_checks_all_history(self):
        r = self.repo()
        r.write("x.core", b"t\n")
        first = r.commit()
        os.remove(r.dir / "x.core")
        tip = r.commit("remove")
        self.assertTrue(blobs.check_push(r.dir, blobs.ZERO, tip))
        self.assertTrue(blobs.check_push(r.dir, "1" * 40, tip))  # a tip this clone never had
        self.assertTrue(blobs.check_history(r.dir, tip))
        self.assertNotEqual(first, tip)

    def test_the_command_exits_non_zero_on_a_problem(self):
        r = self.repo()
        r.write("a.out", ELF)
        r.commit()
        kit = Path(support.ROOT) / "bin" / "kit"
        bad = subprocess.run(["python3", str(kit), "blobs", "--root", str(r.dir)], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1, bad.stdout)
        self.assertIn("a.out", bad.stdout)
        ok = subprocess.run(["python3", str(kit), "blobs"], capture_output=True, text=True, cwd=support.ROOT)
        self.assertEqual(ok.returncode, 0, ok.stdout)

    def test_this_repository_is_clean(self):
        self.assertEqual(blobs.check_index(support.ROOT), [])


if __name__ == "__main__":
    unittest.main()
