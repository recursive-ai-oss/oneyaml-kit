"""A real two-shard quick run of the Go harness on two small workloads merges into the cells of
the unsharded run. Needs a Go toolchain and the modules in its cache."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from oneyaml_bench import report, results, run, shards  # noqa: E402
from test_pipeline_go import build_dir  # noqa: E402

ONLY = ["reference-a", "reference-b"]


class RealShards(unittest.TestCase):
    def go(self, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            return run.run("quick", Path(tmp), ["go"], only=ONLY, build_dir=build_dir(), log=lambda *_: None, **kw)

    def test_two_shards_merge_into_the_cells_of_the_unsharded_run(self):
        whole = self.go()
        parts = [self.go(shard=(i, 2)) for i in (1, 2)]
        # Each shard holds one workload, with every library and task of it.
        self.assertEqual([len(p["run"]["selection"]["groups"]) for p in parts], [1, 1])
        self.assertEqual({p["workloads"][0]["id"] for p in parts}, set(ONLY))
        merged = shards.merge(parts)

        def key(res):
            return sorted((c["language"], c["library"], c["task"], c["workload"], c["status"])
                          for c in res["cells"])

        self.assertEqual(key(merged), key(whole))
        self.assertTrue(whole["cells"])
        results.validate(merged)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("Merged from 2 shards", report.render(merged, Path(tmp)))


if __name__ == "__main__":
    unittest.main()
