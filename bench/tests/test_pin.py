"""A pin that was not applied must not be recorded as one."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneyaml_bench import run  # noqa: E402

H = {"command": ["/x/harness"]}


class Pin(unittest.TestCase):
    def test_pinning_without_taskset_is_an_error(self):
        with mock.patch.object(run.shutil, "which", return_value=None):
            with self.assertRaises(run.HarnessError):
                run.command(H, Path("."), 2)

    def test_pinning_with_taskset_prefixes_the_command(self):
        with mock.patch.object(run.shutil, "which", return_value="/usr/bin/taskset"):
            self.assertEqual(run.command(H, Path("."), 2), ["taskset", "-c", "2", "/x/harness"])

    def test_no_pin_needs_no_taskset(self):
        with mock.patch.object(run.shutil, "which", return_value=None):
            self.assertEqual(run.command(H, Path("."), None), ["/x/harness"])


class KitDigest(unittest.TestCase):
    def tree(self, root: Path, files: dict[str, bytes]) -> None:
        for name, data in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(data)

    def test_the_digest_follows_the_files_that_decide_a_result_and_nothing_else(self):
        import tempfile
        base = {"bench/a.py": b"1", "suites/c/x.yaml": b"a: 1\r\n", "bin/bench": b"#!", "adapters/g/main.go": b"m",
                "tools/t.py": b"t"}
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            self.tree(a, base)
            self.tree(b, base)
            same = run.environment.kit_digest(a)
            # Build output, caches and files outside the listed directories do not count.
            self.tree(b, {"bench/__pycache__/a.pyc": b"x", "adapters/g/bin/out": b"x", "bench/csharp/obj/z": b"x",
                          "docs/readme.md": b"x", "adapters/g/.main.go.swp": b"x"})
            run.environment._digests.clear()
            self.assertEqual(run.environment.kit_digest(b), same)
            for name, data in (("suites/c/x.yaml", b"a: 1\n"), ("bench/new.json", b"{}"), ("bin/bench", b"#!!")):
                with self.subTest(name):
                    self.tree(b, {name: data})
                    run.environment._digests.clear()
                    self.assertNotEqual(run.environment.kit_digest(b), same)
                    self.tree(b, {name: base.get(name, b"")})
                    (b / name).unlink() if name not in base else None
            run.environment._digests.clear()


if __name__ == "__main__":
    unittest.main()
