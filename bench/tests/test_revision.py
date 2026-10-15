"""The kit revision a run records: from its own checkout, or from the launcher for an unpacked archive."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneyaml_bench import environment  # noqa: E402

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def git(d: Path, *args):
    subprocess.run(["git", "-C", str(d), *args], check=True, env=GIT_ENV, capture_output=True)


def make_kit(d: Path):
    for part in environment.DIGEST_DIRS:
        (d / part).mkdir(parents=True, exist_ok=True)
        (d / part / "f.txt").write_text("one\n")
    git(d, "init", "-q", ".")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "m")


class Revision(unittest.TestCase):
    def test_a_clean_checkout_names_its_commit(self):
        with tempfile.TemporaryDirectory() as t:
            kit = Path(t) / "kit"
            kit.mkdir()
            make_kit(kit)
            rev = environment.git_revision(kit, env={})
            self.assertRegex(rev, r"^[0-9a-f]{12}$")

    def test_a_change_in_any_digest_directory_marks_it_modified(self):
        for part in environment.DIGEST_DIRS:
            with self.subTest(part=part), tempfile.TemporaryDirectory() as t:
                kit = Path(t) / "kit"
                kit.mkdir()
                make_kit(kit)
                (kit / part / "f.txt").write_text("two\n")
                self.assertTrue(environment.git_revision(kit, env={}).endswith("+modified"))

    def test_the_launchers_revision_is_ignored_inside_a_checkout_of_the_kit(self):
        with tempfile.TemporaryDirectory() as t:
            kit = Path(t) / "kit"
            kit.mkdir()
            make_kit(kit)
            rev = environment.git_revision(kit, env={"KIT_REVISION": "deadbeef0000"})
            self.assertNotEqual(rev, "deadbeef0000")

    def test_an_unpacked_archive_takes_the_launchers_revision(self):
        with tempfile.TemporaryDirectory() as t:
            kit = Path(t) / "kit"
            kit.mkdir()
            self.assertEqual(environment.git_revision(kit, env={"KIT_REVISION": "abc123def456"}), "abc123def456")
            self.assertEqual(environment.git_revision(kit, env={"KIT_REVISION": "abc123def456+modified"}), "abc123def456+modified")
            self.assertIsNone(environment.git_revision(kit, env={}))

    def test_an_archive_inside_another_repository_does_not_report_that_repository(self):
        with tempfile.TemporaryDirectory() as t:
            outer = Path(t) / "outer"
            outer.mkdir()
            make_kit(outer)                       # the outer repository
            kit = outer / "unpacked"
            kit.mkdir()
            (kit / "bench").mkdir()
            (outer / ".gitignore").write_text("unpacked/\n")
            self.assertIsNone(environment.git_revision(kit, env={}))
            self.assertEqual(environment.git_revision(kit, env={"KIT_REVISION": "abc123def456"}), "abc123def456")


if __name__ == "__main__":
    unittest.main()
