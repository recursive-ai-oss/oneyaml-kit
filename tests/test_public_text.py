import os
import shutil
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from oneyaml_kit import public_text
from oneyaml_kit.public_text import PATTERN, scan


def tmp():
    return Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))


def j(*parts):
    """Build a sample from fragments, so this file itself carries no internal text."""
    return "".join(parts)


class PublicText(unittest.TestCase):
    BAD = [
        j("the ", "ag", "ent did it"), j("see PROC", "EDURE"), j("as the arch", "itect said"),
        j("R", "3 case"), j("P", "1 patch"), j("per \u00a7", "5"), j("path /x/", "al", "era/y"),
        j("uni", "t 4"), j("pha", "se K"), j("a ru", "ling"), j("NN", "-1"), j("Q", "4.4 here"),
        j("/ho", "me/someone"), j("Al", "era"), j("the rev", "iewer"), j("STAND", "ARDS"),
        j("LEARN", "INGS"), j("tier T", "1"), j("pha", "se L", "4"), j("the sw", "arm"),
        j("wai", "ved by own", "er"), j("wai", "vers apply"),
        j("oneyaml", "-inter", "nal"), j("projects/recursive-ai-", "oss/oneyaml/kit"), j("K", "2 notes"),
        j("the Q", "1 rul", "ing"),
    ]
    GOOD = ["plain prose about YAML", "RFC 6902 section 4.3", "a unit of work", "the kit's cases",
            "the section 5.2 of the spec", "r30 is fine", "PR3", "https://github.com/recursive-ai-oss/oneyaml-kit",
            "a standard library", "swap", "a wavy line", "T4 is not a tier", "LT1", "t0 = start()", "l1 cache"]

    def test_patterns_catch_process_text(self):
        for s in self.BAD:
            self.assertTrue(PATTERN.search(s), s)

    def test_patterns_leave_ordinary_text_alone(self):
        for s in self.GOOD:
            self.assertIsNone(PATTERN.search(s), s)

    def test_commit_hashes_only_on_provenance_lines(self):
        t = tmp()
        try:
            (t / "a.md").write_text("see " + "a" * 40 + "\n")
            (t / "b.md").write_text("Revision: " + "a" * 40 + "\n")
            self.assertEqual(len(scan([t / "a.md"], root=t)), 1)
            self.assertEqual(scan([t / "b.md"], root=t), [])
        finally:
            shutil.rmtree(t)

    def test_the_checker_and_its_tests_carry_no_internal_text_themselves(self):
        here = Path(__file__).resolve()
        self.assertEqual(scan([here, Path(public_text.__file__)]), [])

    def test_the_repository_is_clean(self):
        self.assertEqual(scan(public_text.repo_files()), [])


if __name__ == "__main__":
    unittest.main()
