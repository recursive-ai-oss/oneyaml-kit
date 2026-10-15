"""The mode a run records follows the cases it ran."""

import unittest

import support  # noqa: F401
from oneyaml_kit import cases as C
from oneyaml_kit.runner import run_mode


class RunMode(unittest.TestCase):
    def test_fast_without_slow_cases_and_full_with_them(self):
        everything = C.load_own(include_slow=True)
        slow = [c for c in everything if "slow" in c.tags]
        quick = [c for c in everything if "slow" not in c.tags]
        self.assertTrue(slow and quick)
        self.assertEqual(run_mode(quick), "fast")
        self.assertEqual(run_mode(quick + slow[:1]), "full")


if __name__ == "__main__":
    unittest.main()
