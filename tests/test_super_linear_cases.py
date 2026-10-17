"""The size of the cases that guard against super-linear cost.

A bounded case keeps a time limit only as a hang guard, so a case built to catch a reader whose
cost grows faster than its input has to be large enough that such a reader goes over the guard.
This file checks that arithmetic on the case data, not on a clock: it counts the bytes a model
reader would walk or copy, and requires that to need more than the guard even at the speed of a
loop that does nothing else. The rates were measured once with a small C program that walks a
buffer one byte at a time (about 5 GB/s) and one that allocates, copies and frees (about 13 GB/s);
a reader that rescans one byte at a time is slower than both; a vectorised scan can be faster.
Shrinking a recipe or the patch, or raising the guard, makes a test fail.
"""

import json
import unittest

import support  # noqa: F401
from oneyaml_kit import cases as C

SCAN_RATE = 5e9   # bytes per second for a loop that only walks the bytes
COPY_RATE = 13e9  # bytes per second for allocating, copying and freeing a buffer
MARGIN = 1.4      # the model reader must need at least this many times the guard
CASES = {c.id: c for c in C.load_own(include_slow=True)}


def raw(case_id):
    return json.loads((C.OWN_DIR / case_id.split("/", 1)[0] / case_id.split("/", 1)[1] / "case.json").read_bytes())


def guard(case_id):
    return CASES[f"own:{case_id}"].expect["time_seconds"]


def repeats(recipe, text):
    return sum(p.get("repeat", 1) for p in recipe if p.get("text") == text)


class ModelReaderNeedsMoreThanTheGuard(unittest.TestCase):
    def need(self, work_bytes, rate, case_id):
        seconds = work_bytes / rate
        self.assertGreaterEqual(seconds, MARGIN * guard(case_id),
                                f"{case_id}: a model reader needs {seconds:.0f} s, the guard is {guard(case_id)} s")

    def test_implicit_key_window_rescan(self):
        # One walk over the scalar for every open flow sequence.
        cid = "safety/implicit-key-window-rescan"
        recipe = raw(cid)["input_recipe"]
        depth, length = repeats(recipe, "["), repeats(recipe, "x")
        self.assertEqual(repeats(recipe, "]"), depth)
        self.assertEqual(len(CASES[f"own:{cid}"].input), depth + length + depth + 1)
        self.need(depth * length, SCAN_RATE, cid)

    def test_merge_patch_deletions(self):
        # One walk over the whole document for every deleted key.
        cid = "safety/merge-patch-many-deletions-from-a-large-mapping"
        c = CASES[f"own:{cid}"]
        deletions = len(c.args["patch"])
        self.assertEqual(sum(1 for v in c.args["patch"].values() if v is None), deletions)
        self.need(deletions * len(c.input), SCAN_RATE, cid)

    def test_large_appends(self):
        # One pass over the flow sequence built so far for every appended item: the sizes add up
        # to about half of the result for each operation. A copy of the collection is the cheapest
        # such pass, so the copy rate is the one that has to exceed the guard.
        cid = "safety/patch-many-large-appends-into-a-created-flow-sequence"
        c = CASES[f"own:{cid}"]
        ops = len(c.args["patch"]) - 1
        self.assertEqual(ops, 100000)
        self.need(ops * len(c.expect["accept"]["bytes"]) / 2, COPY_RATE, cid)

    def test_tag_prefixes(self):
        # One copy of the prefix for every node that uses it, even if the string is thrown away.
        for name in ("tag-prefix-distinct-suffixes", "tag-prefix-used-by-many-nodes",
                     "tag-prefix-with-alternating-suffixes"):
            cid = f"safety/{name}"
            data = CASES[f"own:{cid}"].input
            first_line = data.split(b"\n", 1)[0]
            prefix = len(first_line) - len(b"%TAG !e! tag:x,2000:")
            nodes = data.count(b"- !e!")
            self.assertEqual(nodes, 64000, cid)
            self.need(nodes * prefix, COPY_RATE, cid)

    def test_deep_flow_mappings_beside_the_fast_case(self):
        # No loop models this cost: the library that was measured needed 3.9 s under 9,000 open
        # mappings and 233 s under 200,000, so the case keeps that depth. The same lines at 9,000
        # levels stay in the fast set as a stored file; the deep form is generated and slow.
        big = CASES["own:safety/deep-flow-mappings-200000-then-many-lines-unclosed"]
        small = CASES["own:safety/deep-flow-mappings-then-many-lines-unclosed"]
        self.assertIn("slow", big.tags)
        self.assertNotIn("slow", small.tags)
        self.assertEqual(big.input.count(b"{"), 200000)
        self.assertEqual(big.input.split(b"{")[-1], small.input.split(b"{")[-1])
        self.assertEqual(big.expect, small.expect)

    def test_many_definitions_of_one_anchor_name(self):
        # One visit to an earlier definition of the name for every alias: the alias after the k-th
        # definition meets k definitions, so n definitions make n(n+1)/2 visits of one step each.
        cid = "safety/many-anchors-sharing-one-name"
        c = CASES[f"own:{cid}"]
        n = repeats(raw(cid)["input_recipe"], "&a 0, *a, ")
        self.assertEqual(c.input.count(b"&a 0"), n)
        self.assertEqual(c.input.count(b"*a,"), n)
        self.assertEqual(c.expect["accept"]["value"], [[0] * (2 * n)])
        self.need(n * (n + 1) / 2, SCAN_RATE, cid)

    def test_anchor_names_sharing_one_hash_stays_small_because_it_cannot_grow(self):
        # All names land in one chain, so the name met in step j is compared with the j-1 names
        # before it, and each comparison reads the two names until they first differ. Names that
        # first differ in stage s share s blocks, so a comparison reads at least s blocks and one
        # byte more. Pairs that first differ in stage s: 2^s prefixes, each with two halves of
        # 2^(k-s-1) names. The model reader needs the margin only at 2^19 names and 53 MB of
        # input, and linear readers then hold more than the memory bound (1 GB) in six of the
        # twelve libraries that can run it, so the case is not resized: it catches only a hang.
        cid = "safety/anchor-names-sharing-one-fnv-hash"
        c = CASES[f"own:{cid}"]
        stages = raw(cid)["input_recipe"][0]["product"]["choices"]
        k, block = len(stages), len(stages[0][0])
        self.assertTrue(all(len(s) == 2 and len(s[0]) == len(s[1]) == block for s in stages))
        self.assertEqual(c.input.count(b"\n"), 2 ** k)
        state = 2166136261
        for a, b in stages:
            ends = set()
            for option in (a, b):
                h = state
                for byte in option.encode():
                    h = ((h ^ byte) * 16777619) & 0xFFFFFFFF
                ends.add(h)
            self.assertEqual(len(ends), 1, (a, b))  # both options leave the hash in the same state
            state = ends.pop()

        def model_bytes(stages_n):
            pairs = [2 ** s * (2 ** (stages_n - s - 1)) ** 2 for s in range(stages_n)]
            self.assertEqual(sum(pairs), 2 ** stages_n * (2 ** stages_n - 1) // 2)
            return sum(p * (block * s + 1) for s, p in enumerate(pairs))

        line = len(c.input) / 2 ** k
        seconds = model_bytes(k) / SCAN_RATE
        self.assertLess(seconds, MARGIN * guard(cid), "the case reaches the margin: say so in its notes")
        # The first size that reaches the margin, and the input it needs.
        need = next(n for n in range(k, 40) if model_bytes(n) / SCAN_RATE >= MARGIN * guard(cid))
        self.assertEqual(need, k + 2)
        self.assertGreater((line + block * (need - k)) * 2 ** need, 50e6)

    def test_duplicate_key_detection_in_a_wide_mapping(self):
        # One comparison with every earlier key. All keys begin with the same text, so a comparison
        # reads at least that text and the byte after it.
        cid = "safety/wide-mapping-600000-keys"
        c = CASES[f"own:{cid}"]
        (part,) = raw(cid)["input_recipe"]
        keys, prefix = part["numbers"]["count"], part["numbers"]["prefix"]
        self.assertEqual(c.input.count(b"\n"), keys)
        self.assertTrue(all(line.startswith(prefix.encode()) for line in c.input.split(b"\n")[:-1]))
        (mapping,) = c.expect["accept"]["value"]
        self.assertEqual(len(mapping), keys)
        self.need(keys * (keys - 1) / 2 * (len(prefix) + 1), SCAN_RATE, cid)


if __name__ == "__main__":
    unittest.main()
