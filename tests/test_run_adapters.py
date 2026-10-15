"""The helper that runs several adapters side by side checks what it can before it builds or starts anything."""

import os
import unittest
from unittest import mock

import support
from oneyaml_kit import runner as R


class RunAdapters(unittest.TestCase):
    def test_an_adapter_missing_its_checkout_is_unavailable_not_a_failed_build(self):
        env = {k: v for k, v in os.environ.items() if k != "KIT_ONEYAML_CSHARP_SRC"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(R, "build") as build:
            with self.assertRaises(R.AdapterUnavailable) as cm:
                support.run_adapters(["oneyaml-csharp"], [], workers=1)
        self.assertIn("KIT_ONEYAML_CSHARP_SRC", str(cm.exception))
        build.assert_not_called()

    def test_adapters_with_an_address_space_limit_are_refused_more_than_one_worker(self):
        self.assertTrue(R.load_manifest("go-yaml-v3").get("memory_limit_mb"))
        with mock.patch.object(R, "build") as build, mock.patch.object(R, "run_adapter") as run:
            with self.assertRaises(ValueError) as cm:
                support.run_adapters(["go-yaml-v3", "go-yaml-v2"], [], workers=2)
        self.assertIn("memory_limit_mb", str(cm.exception))
        build.assert_not_called()
        run.assert_not_called()

    def test_one_worker_is_allowed_for_them(self):
        with mock.patch.object(R, "build"), mock.patch.object(R, "run_adapter", return_value={"x": 1}) as run:
            got = support.run_adapters(["go-yaml-v3"], [], workers=1)
        self.assertEqual(got, {"go-yaml-v3": {"x": 1}})
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
