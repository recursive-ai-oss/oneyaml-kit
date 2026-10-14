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
        j("Pha", "se K: the kit"), j("Pha", "se 2 work"), j("PHA", "SE 2"), j("fixed in k", "7"), j("see k", "5"),
        j("r", "3 case"), j("p", "1 patch"), j("q", "4 here"), j("nn", "-1"),
    ]
    GOOD = ["plain prose about YAML", "RFC 6902 section 4.3", "a unit of work", "the kit's cases",
            "the section 5.2 of the spec", "r30 is fine", "PR3", "https://github.com/recursive-ai-oss/oneyaml-kit",
            "a standard library", "swap", "a wavy line", "T4 is not a tier", "LT1", "t0 = start()", "l1 cache",
            "t1 = time()", "l2 cache", "phase a of the moon", "a phase change", "phase k is not an id", "r30"]

    def test_patterns_catch_process_text(self):
        for s in self.BAD:
            self.assertTrue(PATTERN.search(s), s)

    def test_short_ids_are_caught_in_any_case_but_tier_and_layer_ids_only_in_upper_case(self):
        for s in ("K" + "2 notes", "k" + "2", "R" + "3a", "r" + "3a", "p" + "1", "Q" + "4.4", "nn" + "-1",
                  "see T" + "1", "L" + "4 work"):
            self.assertTrue(PATTERN.search(s), s)
        for s in ("t" + "1", "l" + "4", "t0", "phase" + " k"):
            self.assertIsNone(PATTERN.search(s), s)

    def test_case_data_may_use_short_keys_but_nothing_else(self):
        t = tmp()
        try:
            (t / "suites" / "own").mkdir(parents=True)
            data = t / "suites" / "own" / "input.yaml"
            data.write_text("k" + "1: a\np" + "2: b\n")
            self.assertEqual(scan([data], root=t), [])
            data.write_text("fixed in " + "k" + "7\n# Pha" + "se K\nsee PROC" + "EDURE\n")
            hits = scan([data], root=t)
            self.assertEqual(len(hits), 2, hits)  # the phase id and the private file name; a bare
            # short key is data there
            prose = t / "docs.md"
            prose.write_text("fixed in " + "k" + "7\n")
            self.assertEqual(len(scan([prose], root=t)), 1)
        finally:
            shutil.rmtree(t)

        # Words stay case-insensitive.
        self.assertTrue(PATTERN.search("The AR" + "CHITECT said"))

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


class LicenceFiles(unittest.TestCase):
    """The three licence texts exist and the README points to each of them."""

    FILES = ("LICENSE-MIT", "LICENSE-APACHE", "suites/own/LICENSE")
    MARKS = ("MIT License", "Apache License", "CC0 1.0 Universal")

    def test_the_licence_texts_exist_and_are_named_in_the_readme(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        for f, mark in zip(self.FILES, self.MARKS):
            text = (root / f).read_text(encoding="utf-8")
            self.assertIn(mark, text, f)
            self.assertGreater(len(text), 500, f)
            self.assertIn("](" + f + ")", readme, f)

    def test_the_mit_and_apache_texts_are_scanned_like_any_other_file(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(public_text.LICENSE_TEXTS, {"suites/yaml-test-suite/LICENSE"})
        self.assertEqual(scan([root / "LICENSE-MIT", root / "LICENSE-APACHE"], root), [])
        t = tmp()
        try:
            mit = t / "LICENSE-MIT"
            mit.write_text((root / "LICENSE-MIT").read_text(encoding="utf-8") + j("see the arch", "itect\n"))
            self.assertEqual(len(scan([mit], root=t)), 1)
        finally:
            shutil.rmtree(t)

    def test_the_cc0_text_is_skipped_only_while_it_is_the_standard_text(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(scan([root / public_text.CC0_PATH], root), [])
        t = tmp()
        try:
            cc0 = t / public_text.CC0_PATH
            cc0.parent.mkdir(parents=True)
            standard = (root / public_text.CC0_PATH).read_bytes()
            cc0.write_bytes(standard)
            self.assertEqual(scan([cc0], root=t), [])
            # An edit of any kind brings the file back under the scan, which flags the planted text.
            cc0.write_bytes(standard + j("see the arch", "itect\n").encode())
            self.assertTrue(any(j("arch", "itect") in h for h in scan([cc0], root=t)))
            # Even a harmless edit does: the unedited text contains ordinary legal words the check flags.
            cc0.write_bytes(standard.replace(b"CC0 1.0 Universal", b"CC0 1.0  Universal"))
            self.assertTrue(scan([cc0], root=t))
        finally:
            shutil.rmtree(t)


if __name__ == "__main__":
    unittest.main()
