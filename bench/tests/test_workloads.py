"""Self-tests for the workload set: provenance, pinned digests, the exactness of the edit
expectations, and the determinism of the generators."""

import difflib
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneyaml_bench import gen, workloads as W  # noqa: E402

HERE = Path(__file__).resolve().parent
PINS = json.loads((HERE / "pins.json").read_text(encoding="utf-8"))
LICENCES = {"MIT": "actions-checkout", "CC0-1.0": "docker-awesome-compose",
            "Apache-2.0": None, "CC-BY-4.0": "OAI-learn.openapis.org"}


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Provenance(unittest.TestCase):
    def real(self):
        return sorted(p for p in W.REAL_DIR.iterdir() if p.is_dir() and p.name != "LICENSES")

    def test_every_real_file_has_complete_provenance(self):
        dirs = self.real()
        self.assertGreaterEqual(len(dirs), 7)
        for d in dirs:
            m = json.loads((d / "workload.json").read_text(encoding="utf-8"))
            s = m["source"]
            self.assertEqual(m["id"], d.name)
            self.assertEqual(s["origin"], "upstream")
            self.assertRegex(s["repository"], r"^https://github\.com/[\w.-]+/[\w.-]+$", d.name)
            self.assertRegex(s["revision"], r"^[0-9a-f]{40}$", d.name)
            self.assertTrue(s["path"], d.name)
            self.assertIn(s["licence"], LICENCES, d.name)
            self.assertTrue(m["description"], d.name)

    def test_every_licence_text_is_vendored_for_its_repository(self):
        for d in self.real():
            s = json.loads((d / "workload.json").read_text(encoding="utf-8"))["source"]
            name = s["repository"].replace("https://github.com/", "").replace("/", "-") + ".txt"
            self.assertTrue((W.REAL_DIR / "LICENSES" / name).is_file(), f"licence text for {d.name}")

    def test_sources_page_names_every_workload(self):
        text = (W.REAL_DIR / "SOURCES.md").read_text(encoding="utf-8")
        for d in self.real():
            self.assertIn(f"`{d.name}`", text)

    def test_inputs_and_expectations_match_their_pins(self):
        # A changed byte in a vendored file or a frozen value must be a deliberate, visible change.
        names = {d.name for d in self.real()}
        self.assertEqual(names, set(PINS["real"]), "a workload without a pin, or a pin without a workload")
        for d in self.real():
            pin = PINS["real"][d.name]
            self.assertEqual(sha(d / "input.yaml"), pin["input"], f"{d.name} input.yaml")
            self.assertEqual(sha(d / "expected.json"), pin["expected"], f"{d.name} expected.json")

    def test_licence_texts_match_their_pins(self):
        files = {f.name: sha(f) for f in (W.REAL_DIR / "LICENSES").iterdir()}
        self.assertEqual(files, PINS["licences"], "a licence text changed, appeared or went missing")
        # Apache-2.0 asks that a NOTICE file travel with the work.
        self.assertIn("prometheus-prometheus.NOTICE.txt", files)

    def test_real_inputs_are_small_and_text(self):
        for d in self.real():
            data = (d / "input.yaml").read_bytes()
            self.assertLess(len(data), 1_000_000)
            data.decode("utf-8")

    def test_every_real_file_has_edits_or_says_why_not(self):
        for d in self.real():
            if d.name == "k8s-ingress-nginx-multi":
                continue  # multi-document: an edit targets one document, which this task does not model
            self.assertIn(d.name, W.REAL_EDITS, d.name)


class Expectations(unittest.TestCase):
    def test_a_substitution_must_match_exactly_once(self):
        self.assertEqual(W.apply_subst("a b a", [{"find": "b", "replace": "c"}]), "a c a")
        with self.assertRaisesRegex(W.WorkloadError, "matches 2 times"):
            W.apply_subst("a b a", [{"find": "a", "replace": "c"}])
        with self.assertRaisesRegex(W.WorkloadError, "matches 0 times"):
            W.apply_subst("a b a", [{"find": "z", "replace": "c"}])
        with self.assertRaisesRegex(W.WorkloadError, "matches 0 times"):
            W.apply_subst("a b a", [{"regex": "z+", "replace": "c"}])
        self.assertEqual(W.apply_subst("a b a", [{"find": "a", "replace": "c", "count": 2}]), "c b c")

    def test_edit_expectations_change_only_what_the_edit_names(self):
        # The expected bytes are the input with a few lines changed; a wrong substitution
        # that rewrote the file would show up as a large difference.
        for wid in W.REAL_EDITS:
            src = (W.REAL_DIR / wid / "input.yaml").read_text(encoding="utf-8")
            for key in ("edits", "patch"):
                if key not in W.REAL_EDITS[wid]:
                    continue
                out = W.apply_subst(src, W.REAL_EDITS[wid][key]["subst"], wid)
                a, b = src.splitlines(), out.splitlines()
                ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
                changed = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in ops if tag != "equal")
                self.assertGreater(changed, 0, wid)
                self.assertLess(changed, 24, f"{wid} {key} changed {changed} lines")

    def test_reference_cases_are_the_documented_ones(self):
        a, b = W.reference_a(), W.reference_b()
        out_a = W.apply_subst(a.text, a.edits["subst"])
        out_b = W.apply_subst(b.text, b.edits["subst"])
        self.assertEqual(out_a, "#hello\na: 10\n\n#abc 123\nb: |\n  this is a test2\nc: 13\n")
        self.assertEqual(out_b, "#hello\na: 10\n\n#abc 123\nb: |-\n  this is a test2\nc: 13\n")

    def test_every_patch_has_a_failing_variant_and_matching_edit_ops(self):
        for name, fn in gen.SHAPES.items():
            g = fn(4096)
            if g.patch:
                self.assertIn("failing", g.patch, name)
                self.assertTrue(g.edits, name)


class Generators(unittest.TestCase):
    def test_same_size_gives_same_bytes(self):
        for name, fn in gen.SHAPES.items():
            self.assertEqual(fn(3000).text, fn(3000).text, name)

    def test_pinned_digests_of_small_instances(self):
        # The text is the contract between the Python value and every harness. If a generator
        # changes, every library's expectation changes with it, so the change must be visible.
        got = {name: hashlib.sha256(fn(4096).text.encode("utf-8")).hexdigest() for name, fn in sorted(gen.SHAPES.items())}
        self.assertEqual(got, PINS["generated_4096"])

    def test_sizes_reach_the_target(self):
        for name, fn in gen.SHAPES.items():
            if name.startswith("deep-"):
                continue
            self.assertGreaterEqual(len(fn(20000).text.encode("utf-8")), 20000, name)

    def test_values_are_json_representable_with_string_keys(self):
        def walk(v):
            if isinstance(v, dict):
                for k, x in v.items():
                    self.assertIsInstance(k, str)
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
            else:
                self.assertIsInstance(v, (str, int, float, bool, type(None)))
        for name, fn in gen.SHAPES.items():
            g = fn(2000)
            walk(g.value)
            json.dumps(g.value)

    def test_prepare_writes_a_complete_workload_directory(self):
        with tempfile.TemporaryDirectory() as t:
            metas = W.prepare(Path(t), ("quick",))
            ids = [m["id"] for m in metas]
            self.assertEqual(len(ids), len(set(ids)))
            index = json.loads((Path(t) / "workloads" / "index.json").read_text(encoding="utf-8"))
            self.assertEqual([w["id"] for w in index["workloads"]], ids)
            for m in metas:
                d = Path(t) / "workloads" / m["id"]
                self.assertEqual(sha(d / "input.yaml"), m["input_sha256"])
                self.assertTrue((d / "expected.json").is_file())
                for task in m["tasks"]:
                    if task == "edit-script":
                        self.assertTrue((d / "edit.expected.yaml").is_file(), m["id"])
                    if task == "patch":
                        self.assertTrue((d / "patch.expected.yaml").is_file(), m["id"])
                        ops = json.loads((d / "patch.json").read_text(encoding="utf-8"))
                        self.assertEqual(ops["failing_ops"][:-1], ops["ops"])
                if m["kind"] == "real-world":
                    self.assertRegex(m["source"]["url"], r"^https://github\.com/.+/blob/[0-9a-f]{40}/")
            tiers = {m["tier"] for m in metas}
            self.assertEqual(tiers, {"quick"})

    def test_the_universe_is_what_prepare_writes(self):
        # Known-deviation lists are checked against `universe`, so it must not drift from the
        # workloads a run really has.
        u = W.universe(("quick", "full"))
        with tempfile.TemporaryDirectory() as t:
            for tiers in (("quick",), ("quick", "full")):
                got = {m["id"]: m["tasks"] for m in W.prepare(Path(t), tiers)}
                self.assertEqual(got, {k: u[k] for k in got})
                self.assertEqual(set(got), set(W.universe(tiers)))
        self.assertEqual(len(u), len(set(u)))

    def test_no_workload_lists_a_task_every_library_must_skip(self):
        u = W.universe()
        self.assertNotIn("dump", u["k8s-multi-16k"])
        self.assertNotIn("dump", u["k8s-ingress-nginx-multi"])
        # Merge keys do not hide the dump row: the guard accepts the merged reading.
        self.assertIn("dump", u["compose-merge-2k"])
        self.assertIn("dump", u["compose-merge-1m"])

    def test_edits_have_a_single_expected_outcome(self):
        with tempfile.TemporaryDirectory() as t:
            W.prepare(Path(t), ("quick",))
            names = {f.name for d in (Path(t) / "workloads").iterdir() if d.is_dir() for f in d.iterdir()}
        self.assertFalse([n for n in names if "restyled" in n], "a second accepted outcome for edits")

    def test_full_tier_adds_the_large_inputs(self):
        quick = {w["id"] for w in W.generated_workloads(("quick",))}
        full = {w["id"] for w in W.generated_workloads(("quick", "full"))}
        self.assertTrue(quick < full)
        self.assertIn("records-8m", full - quick)
        self.assertGreaterEqual(max(w["size"] for w in W.generated_workloads(("full",))), 8 * 1024 * 1024)

    def test_ids_are_files_safe(self):
        for w in W.generated_workloads(("quick", "full")):
            self.assertRegex(w["id"], r"^[a-z0-9-]+$")


if __name__ == "__main__":
    unittest.main()
