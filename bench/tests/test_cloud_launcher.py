"""Drives bin/bench-cloud's main() end to end with `aws` and the network stubbed: the arguments are parsed,
the real user_data is called, and the run stops at the launch call, so no instance is ever started."""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "bench-cloud"


class Launched(Exception):
    """Raised by the stub at the launch call, so the run goes no further."""


def load():
    loader = importlib.machinery.SourceFileLoader("bench_cloud_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class MainEndToEnd(unittest.TestCase):
    def run_main(self, *argv):
        """Returns (exit code or None if launched, aws calls, user-data text, stdout)."""
        mod = load()
        calls = []

        def fake_aws(*args, check=True):
            calls.append(args)
            if args[:2] == ("ec2", "run-instances"):
                raise Launched()
            return "ami-test"

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as t:
            env = {k: v for k, v in os.environ.items() if not k.startswith(("KIT_CLOUD_", "AWS_"))}
            env.update(KIT_CLOUD_REGION="r-1", KIT_CLOUD_BUCKET="b", KIT_OUT=t,
                       KIT_CLOUD_CONFIG=str(Path(t) / "none.env"))
            git = mock.Mock(stdout="", returncode=0)
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(sys, "argv", ["bench-cloud", *argv]), \
                    mock.patch.object(mod, "aws", fake_aws), \
                    mock.patch.object(mod, "presign", lambda m, b, r, k, s: f"https://s3.invalid/{m}/{k}"), \
                    mock.patch.object(mod.subprocess, "run", return_value=git), \
                    contextlib.redirect_stdout(out):
                try:
                    code = mod.main()
                except Launched:
                    code = None
                data = list((Path(t) / "cloud").glob("*/user-data.sh"))
                text = data[0].read_text(encoding="utf-8") if data else ""
        return code, calls, text, out.getvalue()

    def bench_line(self, text):
        lines = [x for x in text.split("\n") if "bin/bench run" in x]
        self.assertEqual(len(lines), 1, text)
        return lines[0]

    def test_plain_run_reaches_launch_without_lang(self):
        code, calls, text, _ = self.run_main("quick")
        self.assertIsNone(code)
        self.assertIn(("ec2", "run-instances"), [c[:2] for c in calls])
        line = self.bench_line(text)
        self.assertIn("--mode quick", line)
        self.assertNotIn("--lang", line)

    def test_lang_go_reaches_launch_and_is_passed_on(self):
        code, calls, text, _ = self.run_main("full", "--lang", "go")
        self.assertIsNone(code)
        self.assertIn(("ec2", "run-instances"), [c[:2] for c in calls])
        line = self.bench_line(text)
        self.assertIn("--mode full --lang go --out", line)

    def test_lang_is_repeatable_and_order_free(self):
        code, _, text, _ = self.run_main("full", "--here", "--lang", "rust", "--lang", "csharp", "--lang", "rust")
        self.assertIsNone(code)
        line = self.bench_line(text)
        self.assertEqual(re.findall(r"--lang (\w+)", line), ["rust", "csharp"])
        self.assertIn("shipped-checkout", text)

    def test_unknown_lang_and_stray_arguments_are_refused(self):
        for argv in (("full", "--lang", "cobol"), ("full", "--lang"), ("full", "--bogus"), ("nightly",), ()):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()) as err:
                code, calls, _, _ = self.run_main(*argv)
                self.assertEqual(code, 2)
                self.assertEqual(calls, [])
                if "cobol" in argv:
                    self.assertIn("cobol", err.getvalue())

    def test_user_data_signature_matches_its_caller(self):
        mod = load()
        cfg = dict(mod.DEFAULTS)
        text = mod.user_data(cfg, "quick", ["go"], "put", "log", None, 30)
        self.assertIn("--lang go", text)
        self.assertNotIn("--lang", mod.user_data(cfg, "quick", [], "put", "log", None, 30).split("bin/bench run")[1].split("\n")[0])


if __name__ == "__main__":
    unittest.main()
