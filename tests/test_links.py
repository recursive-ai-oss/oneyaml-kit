import os
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from oneyaml_kit import links


def repo(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


class Links(unittest.TestCase):
    def problems(self, files):
        root = repo(files)
        return links.check(root)

    def test_good_links_pass(self):
        files = {
            "README.md": "[a](docs/a.md) [b](docs/a.md#the-first-part) [c](<docs/a.md#second-part-1>)\n"
                         "[d](LICENSE) [e](docs) [f](https://example.invalid/x.md) [g](#top)\n"
                         "[ref]: docs/a.md#the-first-part\n# Top\n",
            "LICENSE": "x",
            "docs/a.md": "# The `first` part\n## Second part\n## Second part\n[up](../README.md#top)\n",
        }
        self.assertEqual(self.problems(files), [])

    def test_dead_file_is_reported_with_its_line(self):
        got = self.problems({"README.md": "x\n[a](docs/gone.md)\n"})
        self.assertEqual(len(got), 1)
        self.assertIn("README.md:2", got[0])
        self.assertIn("no such file", got[0])

    def test_dead_anchor_is_reported(self):
        got = self.problems({"README.md": "[a](docs/a.md#nope)\n[b](#absent)\n", "docs/a.md": "# Real\n"})
        self.assertEqual(len(got), 2)
        self.assertTrue(all("no heading" in g for g in got))

    def test_dead_reference_definition_is_reported(self):
        self.assertEqual(len(self.problems({"CONTRIBUTING.md": "[x][r]\n\n[r]: missing.md\n"})), 1)

    def test_links_inside_code_are_examples(self):
        text = "```\n[a](nowhere.md)\n```\nand `[b](nowhere.md)` here\n"
        self.assertEqual(self.problems({"README.md": text}), [])

    def test_a_link_out_of_the_repository_is_reported(self):
        self.assertEqual(len(self.problems({"docs/a.md": "[x](../../outside.md)\n"})), 1)

    def test_absolute_urls_are_not_fetched(self):
        self.assertEqual(self.problems({"README.md": "[x](https://example.invalid/404) [m](mailto:a@b.invalid)\n"}), [])

    def test_every_top_level_markdown_file_is_checked(self):
        files = {"SECURITY.md": "[a](gone.md)\n", "CODE_OF_CONDUCT.md": "[b](#nope)\n", "notes.txt": "[c](gone.md)\n"}
        got = self.problems(files)
        self.assertEqual(sorted(g.split(":")[0] for g in got), ["CODE_OF_CONDUCT.md", "SECURITY.md"])

    def test_the_kits_own_docs_have_no_dead_relative_links(self):
        self.assertEqual(links.check(), [])
        self.assertGreaterEqual(len(links.doc_files()), 10)
