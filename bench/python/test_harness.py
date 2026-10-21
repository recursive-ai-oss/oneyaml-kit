"""Tests of the Python harness itself: the common form, the reference reading, the guard, the ownership of
every starting state's input, and the whole path of a cell through its own process with the control
libraries. They check structure and counters, never durations.

Run with the harness's environment: python -m unittest -q test_harness   (from bench/python)
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import sys
import tempfile
import types
import unittest
import unittest.mock
from importlib import metadata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
os.environ["KIT_BENCH_SELFTEST"] = "1"

import cell  # noqa: E402
import common  # noqa: E402
import controls  # noqa: E402
import guard  # noqa: E402
import libs_own  # noqa: E402
import main as harness  # noqa: E402
import measure  # noqa: E402
import referee  # noqa: E402
import registry  # noqa: E402
from oneyaml_bench import run as driver  # noqa: E402
from oneyaml_bench import workloads  # noqa: E402

TMP = None
WL = None


def setUpModule():
    global TMP, WL
    TMP = tempfile.TemporaryDirectory()
    workloads.prepare(Path(TMP.name), ("quick",))
    WL = Path(TMP.name) / "workloads"


def tearDownModule():
    TMP.cleanup()


def workload(wid):
    return common.load_workload(WL / wid)


class CommonForm(unittest.TestCase):
    def test_types_are_exact(self):
        d = common.first_diff
        self.assertEqual(d(1, 1), "")
        self.assertNotEqual(d(1, 1.0), "")
        self.assertNotEqual(d("1", 1), "")
        self.assertNotEqual(d(True, 1), "")
        self.assertNotEqual(d(1, True), "")
        self.assertEqual(d(math.nan, math.nan), "")
        self.assertEqual(d({"a": [1, {"b": None}]}, {"a": [1, {"b": None}]}), "")
        self.assertIn("/a/1", d({"a": [1, 2]}, {"a": [1, 3]}))
        self.assertIn("missing /b", d({"a": 1}, {"a": 1, "b": 2}))
        self.assertIn("unexpected /c", d({"a": 1, "c": 3}, {"a": 1}))

    def test_a_deep_value_is_compared_without_recursion(self):
        a = b = []
        for _ in range(50000):
            a.append([])
            a = a[0]
        c = c0 = []
        for _ in range(50000):
            c.append([])
            c = c[0]
        self.assertEqual(common.first_diff(b, c0), "")

    def test_library_values_become_the_common_form(self):
        class ScalarBoolean(int):
            pass

        v = common.to_common({1: [True, ScalarBoolean(1), 2.5, "x"], None: None, "k": 0})
        self.assertEqual(v, {"1": [True, True, 2.5, "x"], "null": None, "k": 0})
        self.assertEqual(common.key_string(True), "true")
        self.assertIs(type(v["1"][1]), bool)
        import datetime
        self.assertEqual(common.to_common(datetime.date(2020, 1, 2)), "timestamp 2020-01-02")
        with self.assertRaises(common.CompareError):
            common.to_common({1, 2})

    def test_bytes_diff_names_the_kind_of_difference(self):
        self.assertEqual(common.bytes_diff(b"a: 1\n", b"a: 1\n"), "")
        self.assertIn("quoting", common.bytes_diff(b"a: 'x'\n", b"a: x\n"))
        self.assertIn("indentation", common.bytes_diff(b"a:\n  b: 1\n", b"a:\n    b: 1\n"))


class CoreResolver(unittest.TestCase):
    def test_core_schema(self):
        r = referee.resolve_core
        for text, want in (("", None), ("~", None), ("Null", None), ("true", True), ("FALSE", False),
                           ("017", 17), ("+5", 5), ("0o17", 15), ("0x1F", 31), ("1_000", "1_000"),
                           ("yes", "yes"), ("on", "on"), ("1.5", 1.5), (".5", 0.5), ("1e3", 1000.0),
                           (".inf", math.inf), ("-.INF", -math.inf), ("0b101", "0b101"), ("2001-01-23", "2001-01-23"),
                           ("1:20", "1:20"), ("12abc", "12abc")):
            got = r(text)
            self.assertEqual((type(got), got), (type(want), want), text)
        self.assertTrue(math.isnan(r(".NaN")))

    def test_tags(self):
        self.assertEqual(referee.resolve_tagged("tag:yaml.org,2002:float", "1"), 1.0)
        self.assertIs(type(referee.resolve_tagged("tag:yaml.org,2002:str", "12")), str)
        with self.assertRaises(common.CompareError):
            referee.resolve_tagged("tag:yaml.org,2002:int", "x")
        with self.assertRaises(common.CompareError):
            referee.resolve_tagged("!local", "x")


class Referee(unittest.TestCase):
    def read(self, text):
        return referee.two_referees(text.encode())

    def test_structure_from_two_parsers_meaning_from_the_resolver(self):
        v = self.read("a: &x [1, '1', \"2\", 1.0, yes, ~]\nb: *x\nc: !!str 12\nd: |\n  lit\n<<: {z: 1}\n")
        self.assertEqual(v["a"], [1, "1", "2", 1.0, "yes", None])
        self.assertEqual(v["b"], v["a"])
        self.assertEqual(v["c"], "12")
        self.assertEqual(v["d"], "lit\n")
        self.assertEqual(v["<<"], {"z": 1})      # a merge key is an ordinary key

    def test_streams_and_empty_documents(self):
        self.assertEqual(self.read("a: 1\n---\nb: 2\n"), [{"a": 1}, {"b": 2}])
        self.assertEqual(self.read("a: 1\n"), {"a": 1})

    def test_text_that_is_not_yaml_is_rejected_by_both(self):
        with self.assertRaises(referee.RefereeError) as c:
            self.read("a: [1, 2\nb: }\n")
        self.assertTrue(c.exception.rejected)

    def test_a_deeply_nested_text_is_read_without_recursion(self):
        text = "[" * 300 + "]" * 300
        v = self.read(text)
        for _ in range(299):
            v = v[0]
        self.assertEqual(v, [])

    def test_disagreement_is_unjudged_not_rejected(self):
        # When only one of the two parsers refuses a text, nothing says which is right: the cell is
        # "unjudged", never a verdict on the library.
        calls = {"n": 0}

        def fake(text):
            calls["n"] += 1
            raise ValueError("refused")

        real = referee.read_libyaml
        referee.read_libyaml = fake
        try:
            with self.assertRaises(referee.RefereeError) as c:
                self.read("a: 1\n")
        finally:
            referee.read_libyaml = real
        self.assertFalse(c.exception.rejected)
        self.assertEqual(calls["n"], 1)


class Guard(unittest.TestCase):
    """Every control through every task: right is accepted, wrong is refused, a refusal has no number."""

    WORKLOADS = ("reference-a", "reference-b", "gha-workflow", "compose-merge-2k")

    def test_controls_through_every_task(self):
        checked = set()
        for wid in self.WORKLOADS:
            w = workload(wid)
            for task in w.tasks:
                for lib in controls.controls():
                    if lib.behaviour == controls.CRASH:
                        continue      # ends the process; covered by the cell tests
                    r, why = lib.runner(task, w)
                    v = cell.check(r, task, w)
                    want = {"control-correct": "ok", "control-wrong": "differs", "control-reject": "rejects"}[lib.id]
                    # A dump the reference parsers read differently is the harness's limit, not a verdict.
                    if v.status == "unjudged":
                        continue
                    self.assertEqual(v.status, want, f"{lib.id} {task} {wid}: {v.detail}")
                    self.assertEqual(bool(v.detail), want != "ok")
                    checked.add((lib.id, task))
        for task in common.TASKS:
            self.assertIn(("control-correct", task), checked)
            self.assertIn(("control-wrong", task), checked)

    def test_a_patch_that_does_not_fail_is_refused(self):
        w = workload("reference-a")
        r, _ = controls.Control("c", controls.WRONG).runner("patch", w)
        bad = common.Product(common.BYTES, data=w.patch_expected[0][1])
        v = guard.judge("patch", w, bad, None, common.FailedPatch(False, w.input), referee.two_referees)
        self.assertEqual(v.status, "differs")
        self.assertIn("failing step", v.detail)
        v = guard.judge("patch", w, bad, None, common.FailedPatch(True, w.input + b" "), referee.two_referees)
        self.assertIn("changed the document", v.detail)

    def test_a_dump_is_read_back_and_judged_whole(self):
        w = workload("reference-a")
        good = common.Product(common.BYTES, data=b'a: 10\nb: "this is a test\\n"\nc: 13\n')
        self.assertEqual(guard.judge("dump", w, good, None, None, referee.two_referees).status, "ok")
        wrong = common.Product(common.BYTES, data=b'a: "10"\nb: "this is a test\\n"\nc: 13\n')
        v = guard.judge("dump", w, wrong, None, None, referee.two_referees)
        self.assertEqual(v.status, "differs")
        self.assertIn("/a", v.detail)
        junk = common.Product(common.BYTES, data=b"a: [1\n")
        self.assertEqual(guard.judge("dump", w, junk, None, None, referee.two_referees).status, "differs")

    def test_merge_variant_and_value_only(self):
        w = workload("compose-merge-2k")
        self.assertEqual([n for n, _ in w.expected], ["spec", "merged"])
        merged = common.Product(common.VALUE, w.expected[1][1], True)
        v = guard.judge("generic-load", w, merged, None, None, referee.two_referees)
        self.assertEqual((v.status, v.variant), ("ok", "merged"))
        w = workload("reference-a")
        p = common.Product(common.BYTES, data=b"a: 10\nb: |\n  this is a test2\nc: 13\n", value_only=True)
        v = guard.judge("patch", w, p, None, common.FailedPatch(True, w.input), referee.two_referees)
        self.assertEqual((v.status, v.variant), ("ok", "value-only"))


class Libraries(unittest.TestCase):
    def test_controls_are_listed_only_on_request(self):
        self.assertIn("control", {l.role for l in registry.all_libraries()})
        del os.environ["KIT_BENCH_SELFTEST"]
        try:
            self.assertNotIn("control", {l.role for l in registry.all_libraries()})
        finally:
            os.environ["KIT_BENCH_SELFTEST"] = "1"

    def test_the_installed_versions_are_the_pins(self):
        pins = {}
        for line in (HERE / "libraries.in").read_text().splitlines():
            if "==" in line and not line.startswith("#"):
                name, ver = line.split("==")
                pins[name.lower().replace("_", "-")] = ver
        self.assertGreaterEqual(len(pins), 9)
        for name, ver in pins.items():
            self.assertEqual(metadata.version(name), ver, name)
        for lib in registry.all_libraries():
            if lib.role == "competitor":
                self.assertEqual(registry.version(lib), pins[lib.dist.lower().replace("_", "-")], lib.id)

    def test_every_library_answers_every_task_it_takes_part_in(self):
        for wid in ("reference-a", "records-4k"):
            w = workload(wid)
            for lib in registry.all_libraries():
                if lib.role == "control":
                    continue
                for task in w.tasks:
                    if task not in lib.tasks():
                        continue
                    r, why = lib.runner(task, w)
                    if r is None:
                        self.assertTrue(why, f"{lib.id} {task} is unsupported without a reason")
                        continue
                    v = cell.check(r, task, w)
                    self.assertIn(v.status, ("ok", "differs", "rejects", "unjudged"), f"{lib.id} {task}")

    def test_a_competitor_that_gets_it_right_gets_it_right(self):
        # One answer that must be right everywhere, so a harness fault shows as a failure here.
        w = workload("records-4k")
        for lib_id in ("ruamel-safe", "yamlrocks", "ryaml", "py-yaml12", "yaml-rs", "pyyaml-c", "pyyaml-pure"):
            r, _ = registry.find(lib_id).runner("generic-load", w)
            self.assertEqual(cell.check(r, "generic-load", w).status, "ok", lib_id)
        r, _ = registry.find("yamlrocks-roundtrip").runner("unchanged-emit", w)
        self.assertEqual(cell.check(r, "unchanged-emit", w).status, "ok")

    def test_typed_load_reaches_the_model(self):
        w = workload("records-4k")
        r, _ = registry.find("ryaml").runner("typed-load", w)
        out = r.run(r.setup())
        self.assertIsInstance(out, list)
        self.assertEqual(type(out[0]).__name__, "Record")
        self.assertEqual(type(out[0].address.geo).__name__, "Geo")
        self.assertEqual(r.product(out).value, w.expected[0][1])

    def test_edit_steps_use_an_index_in_a_sequence_and_a_key_in_a_mapping(self):
        w = workload("gha-workflow")
        steps = common.resolve_steps(w, w.edit_ops)
        self.assertEqual([(s.op, [(g.key, g.index, g.is_idx) for g in s.segs]) for s in steps][2],
                         ("replace", [("jobs", 0, False), ("build", 0, False), ("steps", 0, False),
                                      ("", 0, True), ("with", 0, False), ("node-version", 0, False)]))


# ---- the input of every starting state is the state's own ---------------------------------------

def reachable(obj, min_len):
    """(strings and byte strings of at least min_len that can be reached from obj, non-empty containers):
    each by identity. Classes, modules and functions are not followed."""
    skip = (type, types.ModuleType, types.FunctionType, types.BuiltinFunctionType, types.CodeType, types.MethodType)
    seen, buffers, containers, stack = set(), {}, {}, [obj]
    while stack:
        o = stack.pop()
        if id(o) in seen or isinstance(o, skip):
            continue
        seen.add(id(o))
        if isinstance(o, (str, bytes, bytearray)):
            if len(o) >= min_len:
                buffers[id(o)] = o
            continue
        if isinstance(o, (list, dict, set)) and len(o):
            containers[id(o)] = o
        stack.extend(gc.get_referents(o))
    return buffers, containers


def sharing(states, master, min_len, must_hold):
    """The first problem with the input a batch of states holds, or ""."""
    found = [reachable(s, min_len) for s in states]
    for i, (bufs, _) in enumerate(found):
        if must_hold and not bufs:
            return f"state {i} holds no copy of the input its task reads"
        if id(master) in bufs:
            return f"state {i} reads the workload's own input"
    for i in range(len(found)):
        for j in range(i + 1, len(found)):
            if found[i][0].keys() & found[j][0].keys():
                return f"states {i} and {j} share an input buffer"
            if found[i][1].keys() & found[j][1].keys():
                return f"states {i} and {j} reach the same parsed object"
    return ""


class Ownership(unittest.TestCase):
    def test_no_two_prepared_states_share_an_input(self):
        w = workload("records-4k")
        checked = set()
        for lib in registry.all_libraries():
            if lib.role == "control":
                continue
            for task in w.tasks:
                if task not in lib.tasks():
                    continue
                r, _ = lib.runner(task, w)
                if r is None:
                    continue
                states = [r.setup() for _ in range(4)]
                reads = task in ("typed-load", "generic-load", "document-parse", "patch")
                why = sharing(states, w.input, len(w.input) // 2, reads)
                self.assertEqual(why, "", f"{lib.id} {task}")
                checked.add((lib.id, task))
        self.assertGreaterEqual(len(checked), 30)
        for pair in (("ruamel-roundtrip", "unchanged-emit"), ("yamlrocks", "generic-load"),
                     ("yaml-edit", "document-parse"), ("pyyaml-pure", "generic-load")):
            self.assertIn(pair, checked)

    def test_the_check_finds_shared_buffers(self):
        master = b"x" * 100
        copy1, copy2 = common.copy_bytes(master), common.copy_bytes(master)
        own = lambda b: common.State(b)      # noqa: E731
        self.assertEqual(sharing([own(copy1), own(copy2)], master, 50, True), "")
        self.assertIn("share an input buffer", sharing([own(copy1), own(copy1)], master, 50, True))
        self.assertIn("workload's own", sharing([own(copy1), own(master)], master, 50, True))
        self.assertIn("share an input buffer",
                      sharing([common.State(None, None, [copy1]), common.State(None, None, {"k": copy1})], master, 50, False))
        shared = {"a": 1}
        self.assertIn("same parsed object",
                      sharing([common.State(None, None, shared), common.State(None, None, shared)], master, 50, False))
        self.assertIn("holds no copy", sharing([common.State(None), common.State(None)], master, 50, True))

    def test_copies_are_new_objects(self):
        b = b"abc" * 100
        self.assertIsNot(common.copy_bytes(b), b)
        self.assertEqual(common.copy_bytes(b), b)
        self.assertIsNot(common.copy_text(b), common.copy_text(b))


# ---- a cell through its own process ---------------------------------------------------------

def args(**kw):
    return argparse.Namespace(workloads=str(WL), mode="quick", **kw)


class Cells(unittest.TestCase):
    def cell(self, lib_id, task, wid="reference-a"):
        return harness.run_cell(registry.find(lib_id), task, wid, args())

    def test_a_right_answer_is_timed_and_measured(self):
        rec = self.cell("ryaml", "generic-load")
        self.assertEqual(rec["status"], "ok", rec)
        self.assertEqual(rec["variant"], "spec")
        self.assertGreater(rec["time"]["ns"], 0)
        self.assertEqual(rec["time"]["stat"], "mean")
        self.assertIsInstance(rec["memory"]["allocated_bytes"], int)
        self.assertIsNone(rec["memory"]["allocations"])
        self.assertEqual(rec["memory"]["allocated_bytes"] % 4096, 0)

    def test_a_wrong_or_missing_answer_has_no_number(self):
        for lib, status in (("control-wrong", "differs"), ("control-reject", "rejects"),
                            ("ruamel-json-merge-patch", "unsupported")):
            task = "patch" if lib.startswith("ruamel") else "generic-load"
            rec = self.cell(lib, task)
            self.assertEqual(rec["status"], status, rec)
            self.assertTrue(rec["detail"])
            self.assertNotIn("time", rec)
            self.assertNotIn("memory", rec)

    def test_a_process_that_dies_is_an_error_without_a_number(self):
        rec = self.cell("control-crash", "generic-load")
        self.assertEqual(rec["status"], "error")
        self.assertIn("exit status 3", rec["detail"])
        self.assertNotIn("time", rec)

    def test_the_memory_of_a_run_grows_with_the_work(self):
        small = self.memory("control-correct", "unchanged-emit", "reference-a")
        self.assertEqual(small % 4096, 0)
        big = self.memory("ruamel-roundtrip", "document-parse", "records-64k")
        small_doc = self.memory("ruamel-roundtrip", "document-parse", "reference-a")
        self.assertGreater(big, small_doc)

    def memory(self, lib, task, wid):
        import subprocess
        r = subprocess.run([sys.executable, str(HERE / "memchild.py"), "--workloads", str(WL), "--library", lib,
                            "--task", task, "--workload", wid], capture_output=True, text=True,
                           env={**os.environ, "PYTHONMALLOC": "malloc"})
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["bytes"]

    def test_the_run_writes_the_protocol(self):
        out = Path(TMP.name) / "out.jsonl"
        a = argparse.Namespace(workloads=str(WL), mode="quick", out=str(out), library=["ryaml", "control-wrong"],
                               task=["generic-load"], workload=["reference-a"], skip=[])
        harness.run(a)
        harness_rec, cells, begun = driver.parse_lines(out)
        self.assertEqual(harness_rec["language"], "python")
        self.assertEqual(harness_rec["text_tasks"], ["patch"])
        self.assertIn("resident set", harness_rec["memory"])
        self.assertTrue(harness_rec["tool"].startswith("pyperf "))
        self.assertEqual(begun, ["ryaml/generic-load/reference-a", "control-wrong/generic-load/reference-a"])
        self.assertEqual([c["status"] for c in cells], ["ok", "differs"])
        self.assertEqual({l["id"]: l["role"] for l in harness_rec["libraries"]},
                         {"ryaml": "competitor", "control-wrong": "control"})
        # The skipped cell is not begun.
        a.skip = ["control-wrong/generic-load/reference-a"]
        harness.run(a)
        self.assertEqual(driver.parse_lines(out)[2], ["ryaml/generic-load/reference-a"])


class OwnLibraries(unittest.TestCase):
    def test_the_backend_is_chosen_before_the_package_is_imported(self):
        import libs_own
        fake = types.ModuleType("oneyaml")
        seen = []
        fake.__dict__["seen"] = seen
        with unittest.mock.patch.dict(sys.modules, {"oneyaml": fake}), unittest.mock.patch.dict(os.environ, {}):
            for backend, want in (("pure", "pure"), ("native", None)):
                lib = libs_own.OwnLib("x", backend, "abc")
                for task in common.TASKS:
                    # Every task has a runner; the library is only called when it runs.
                    r, why = lib.runner(task, workload("reference-a"))
                    self.assertIsNotNone(r, (task, why))
                self.assertEqual(os.environ.get("ONEYAML_BACKEND"), want)
        self.assertEqual(registry.version(libs_own.OwnLib("x", "pure", "abc")), "abc")

    def test_the_library_is_listed_only_when_the_build_installed_it(self):
        with tempfile.TemporaryDirectory() as t:
            rec = Path(t) / "own.json"
            with unittest.mock.patch.object(libs_own, "RECORD", rec):
                self.assertEqual(libs_own.own_libraries(), [])
            rec.write_text('{"revision": "abc123"}')
            with unittest.mock.patch.object(libs_own, "RECORD", rec):
                libs = libs_own.own_libraries()
        self.assertEqual([(l.id, l.role, l.revision) for l in libs],
                         [("oneyaml-python", "own", "abc123"), ("oneyaml-python-pure", "own", "abc123")])


class Measure(unittest.TestCase):
    def test_pyperf_settings_only_shorten_pyperf(self):
        self.assertEqual(harness.pyperf_args("full"), [])
        self.assertEqual(harness.pyperf_args("quick")[::2], ["--processes", "--values", "--warmups", "--min-time"])

    def test_time_func_builds_every_state_outside_the_clock_and_runs_each_once(self):
        built, ran = [], []

        def setup():
            built.append(1)
            return common.State(len(built))

        r = common.Runner(setup, lambda s: ran.append(s.input), lambda o: o)
        total = measure.time_func(40, r)
        self.assertGreaterEqual(total, 0)
        self.assertEqual(len(built), 40)
        self.assertEqual(sorted(ran), list(range(1, 41)))

    def test_the_loops_are_capped_when_building_states_costs_more_than_the_runs(self):
        # A fake clock that the starting state and the run advance by a fixed amount, so the choice is a
        # function of counters and not of the machine.
        now = [0.0]

        def runner(setup_s, run_s):
            def setup():
                now[0] += setup_s
                return common.State(None)

            def run(_):
                now[0] += run_s

            return common.Runner(setup, run, lambda o: o)

        real = measure.time.perf_counter
        measure.time.perf_counter = lambda: now[0]
        try:
            # 50 ms of runs is 50000 loops of 1 us; at 1 ms a state that is 50 s of setup: capped to the budget.
            self.assertEqual(measure.capped_loops(runner(1e-3, 1e-6), 0.05), int(measure.SETUP_BUDGET * 0.05 / (1e-3 + 1e-6)))
            # Cheap states: pyperf's own calibration is left alone.
            self.assertEqual(measure.capped_loops(runner(1e-6, 1e-6), 0.05), 0)
            # Never fewer than a few loops, however costly the state.
            self.assertEqual(measure.capped_loops(runner(10.0, 1e-6), 0.05), measure.MIN_LOOPS)
        finally:
            measure.time.perf_counter = real

    def test_the_records_hold_no_text_that_cannot_be_published(self):
        a = argparse.Namespace(mode="quick")
        rec = harness.record(a, [l for l in registry.all_libraries() if l.role == "competitor"])
        self.assertEqual(driver.record_hits({k: v for k, v in rec.items() if k != "type"}), [])


if __name__ == "__main__":
    unittest.main()
