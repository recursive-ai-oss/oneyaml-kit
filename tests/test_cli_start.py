"""An adapter that cannot start fails alone: the command line says so and the others still run."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401
from oneyaml_kit import cli
from oneyaml_kit import runner as R
from oneyaml_kit.cases import load_all
from test_runner import mk

FAKE = str(Path(__file__).parent / "fixtures" / "fake_adapter.py")


class FirstStart(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, self.t)
        (self.t / "script.json").write_text("{}")
        self.dir = self.t / "adapters"
        for name, command in (("broken", [sys.executable, "-c", "print('not a hello')"]),
                              ("silent", [sys.executable, "-c", "pass"]),
                              ("good", [sys.executable, FAKE])):
            d = self.dir / name
            d.mkdir(parents=True)
            (d / "adapter.json").write_text(json.dumps({
                "format": R.ADAPTER_FORMAT, "name": name, "language": "python", "command": command,
                "timeout_seconds": 2}))

    def kit(self, *adapters):
        out = self.t / "out"
        argv = ["fast", "--suite", "own", "--case", load_all()[0].id, "--out", str(out)]
        for a in adapters:
            argv += ["--adapter", a]
        real = R.run_adapter
        env = dict(os.environ, FAKE_SCRIPT=str(self.t / "script.json"))
        so, se = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "run_adapter", lambda n, *a, **k: real(n, *a, adapters_dir=self.dir, env=env, **k)):
            with mock.patch.object(cli, "list_adapters", lambda: sorted(p.name for p in self.dir.iterdir())):
                with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
                    code = cli.main(argv)
        return code, so.getvalue(), se.getvalue(), out

    def test_an_adapter_that_cannot_say_hello_is_a_clean_failure(self):
        for name in ("broken", "silent"):
            code, so, se, out = self.kit(name)
            self.assertEqual(code, 1, name)
            self.assertIn(f"{name}: FAILED, the adapter did not start", se)
            self.assertFalse((out / "results" / f"{name}.json").exists())

    def test_the_other_adapters_still_run(self):
        code, so, se, out = self.kit("broken", "good", "silent")
        self.assertEqual(code, 1)
        self.assertEqual(se.count("FAILED, the adapter did not start"), 2)
        self.assertIn("fake 0:", so)
        self.assertTrue((out / "results" / "good.json").is_file())
        self.assertTrue((out / "conformance.md").is_file())

    def test_a_run_holds_a_restarted_adapter_to_the_hello_it_accepted(self):
        # Through run_adapter, which is what records the first hello: the case that crashes needs a
        # restart, the restart says another version, and neither it nor the next case runs.
        (self.t / "script.json").write_text(json.dumps({"b": "crash"}))
        env = dict(os.environ, FAKE_SCRIPT=str(self.t / "script.json"), FAKE_STARTS=str(self.t / "starts"),
                   FAKE_LATER=json.dumps({"version": "1"}))
        res = R.run_adapter("good", [mk("b"), mk("c")], False, adapters_dir=self.dir, env=env)
        self.assertEqual([r["loose"] for r in res["cases"]], ["fail", "fail"])
        self.assertIn("hello differs from the first start", res["cases"][1]["detail"])
        self.assertEqual(res["run"]["adapter_restarts"], 0)


if __name__ == "__main__":
    unittest.main()
