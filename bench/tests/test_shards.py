"""Sharded runs: a deterministic plan, shards that hold whole comparison rows, and a merge that is
never more publishable than the worst of its shards."""

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneyaml_bench import cli, report as P, results as R, run, shards  # noqa: E402

LIBS = {"go": [("ours", "own"), ("rival", "competitor"), ("weak", "competitor")],
        "rust": [("rs-ours", "own"), ("rs-rival", "competitor")]}
METAS = [
    {"id": "w1", "kind": "generated", "tier": "quick", "description": "d", "documents": 1, "input_bytes": 4000,
     "tasks": ["generic-load", "dump"], "source": {}},
    {"id": "w2", "kind": "generated", "tier": "quick", "description": "d", "documents": 1, "input_bytes": 90000,
     "tasks": ["generic-load"], "source": {}},
    {"id": "w3", "kind": "generated", "tier": "quick", "description": "d", "documents": 1, "input_bytes": 2000000,
     "tasks": ["generic-load", "dump", "patch"], "source": {}},
]
OFFERED = [{"id": m["id"], "bytes": m["input_bytes"], "tasks": m["tasks"]} for m in METAS]
ENV = {"date": "2026-01-01T00:00:00Z", "governor": "performance", "pinned_cpu": 2, "cpu": "Test CPU", "cpus": 4,
       "memory_gb": 8.0, "os": "Linux", "machine": "x86_64", "boost": "disabled", "python": "3.13.0",
       "kit_revision": "abc", "instance_type": "c7.large"}


def cell(lib, task, wl):
    return {"type": "cell", "library": lib, "task": task, "workload": wl, "status": "ok", "input_bytes": 1,
            "time": {"median_ns": 1.0, "mad_ns": 0.0, "samples": 31},
            "memory": {"method": "m", "peak_bytes": 1, "allocated_bytes": 1, "allocations": 1}}


def fake_run_harness(h, build_dir, wl_dir, mode, out_dir, sampler, pin_cpu, libraries, tasks, only, log,
                     skip_cells=()):
    lang = h["language"]
    libs = [(i, r) for i, r in LIBS[lang] if not libraries or i in libraries]
    rec = {"type": "harness", "name": "h", "version": "1", "language": lang, "runtime": "rt", "target": "t",
           "flags": {}, "config": {"warmup_ms": 1, "samples": 5, "sample_target_ms": 1}, "timer": "t", "memory": "m",
           "libraries": [{"id": i, "package": i, "version": "1", "role": r, "settings": {}} for i, r in libs]}
    cells = [cell(i, t, m["id"]) for m in METAS if not only or m["id"] in only
             for t in m["tasks"] if not tasks or t in tasks for i, _ in libs]
    return rec, cells, 0


class FakeSampler:
    def start(self): pass
    def watch(self, pid): pass
    def stop(self): return [0.1] * 10


def go(mode="full", languages=(), shard=None, env=None, load=0.5, **kw):
    harnesses = {l: {"language": l, "_dir": Path(".")} for l in LIBS}
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(run, "discover", return_value=harnesses), \
            mock.patch.object(run, "build"), \
            mock.patch.object(run, "run_harness", fake_run_harness), \
            mock.patch.object(shards, "offered_workloads", return_value=[dict(w) for w in OFFERED]), \
            mock.patch.object(run.workloads, "prepare", return_value=[dict(m) for m in METAS]), \
            mock.patch.object(run.environment, "Sampler", FakeSampler), \
            mock.patch.object(run.environment, "loadavg", return_value=(load, load, load)), \
            mock.patch.object(run.environment, "describe", return_value=dict(env or ENV)), \
            mock.patch.object(run.environment, "git_revision", return_value="abc"):
        return run.run(mode, Path(tmp), list(languages), pin_cpu=2, memory_processes=1,
                       log=lambda *_: None, shard=shard, **kw)


def shard_set(n=3, **kw):
    return [go(shard=(i, n), **kw) for i in range(1, n + 1)]


def pub(res):
    return res["run"]["publishability"]


def names(cells):
    return {(c["language"], c["library"], c["task"], c["workload"], c["status"]) for c in cells}


def merge_fails(test, parts, text, **kw):
    with test.assertRaises(shards.MergeError) as cm:
        shards.merge(parts, **kw)
    test.assertTrue([p for p in cm.exception.problems if text in p], cm.exception.problems)


class Plan(unittest.TestCase):
    def plan(self, n=3, **kw):
        passes = {"go": 4, "rust": 4}
        return shards.build_plan(passes, OFFERED, n, ("quick", "full"),
                                 {"libraries": [], "tasks": [], "workloads": [], **kw})

    def test_the_same_inputs_give_the_same_plan_and_digest(self):
        a, b = self.plan(), self.plan()
        self.assertEqual(a, b)
        self.assertEqual(a["plan_digest"], b["plan_digest"])

    def test_any_input_that_changes_the_work_changes_the_digest(self):
        base = self.plan()["plan_digest"]
        self.assertNotEqual(base, self.plan(2)["plan_digest"])
        self.assertNotEqual(base, self.plan(tasks=["generic-load"])["plan_digest"])
        other = [dict(w) for w in OFFERED]
        other[0]["bytes"] += 1
        p = shards.build_plan({"go": 4, "rust": 4}, other, 3, ("quick", "full"),
                              {"libraries": [], "tasks": [], "workloads": []})
        self.assertNotEqual(base, p["plan_digest"])

    def test_every_group_is_in_exactly_one_shard_and_rows_are_not_split(self):
        p = self.plan()
        self.assertEqual(sorted(p["groups"]), sorted(f"{l}/{w['id']}" for l in LIBS for w in OFFERED))
        seen = [g for i in (1, 2, 3) for g in shards.shard_groups(p, i)]
        self.assertEqual(sorted(seen), sorted(p["groups"]))
        # A group is a language on a workload: all its tasks stay together.
        for g, v in p["groups"].items():
            self.assertEqual(v["tasks"], next(w["tasks"] for w in OFFERED if w["id"] == g.split("/")[1]))

    def test_the_shards_are_balanced_by_cost(self):
        p = self.plan()
        d = shards.describe(p)
        costs = [s["estimated_cost"] for s in d["shards"]]
        biggest = max(g["cost"] for g in p["groups"].values())
        self.assertLessEqual(max(costs) - min(costs), biggest)
        self.assertTrue(all(s["groups"] for s in d["shards"]))

    def test_more_shards_than_rows_is_an_error(self):
        with self.assertRaises(shards.PlanError):
            self.plan(7)

    def test_the_real_workload_set_plans_into_four_shards(self):
        tiers = ("quick", "full")
        offered = shards.offered_workloads(tiers)
        p = shards.build_plan({"go": 4, "rust": 4, "csharp": 8}, offered, 4, tiers,
                              {"libraries": [], "tasks": [], "workloads": []})
        self.assertEqual(len(p["groups"]), 3 * len(offered))
        self.assertEqual(sum(len(shards.shard_groups(p, i)) for i in range(1, 5)), len(p["groups"]))


class ShardRun(unittest.TestCase):
    def test_a_shard_measures_whole_rows_and_is_not_publishable_alone(self):
        res = go(shard=(1, 3))
        rec = res["run"]["shard"]
        self.assertEqual((rec["index"], rec["count"]), (1, 3))
        self.assertTrue(rec["plan_digest"].startswith("sha256:"))
        mine = set(res["run"]["selection"]["groups"])
        self.assertTrue(mine)
        self.assertEqual({f"{c['language']}/{c['workload']}" for c in res["cells"]}, mine)
        for g in mine:  # every library and every task of the row is there
            lang, wl = g.split("/")
            libs = {c["library"] for c in res["cells"] if c["language"] == lang and c["workload"] == wl}
            self.assertEqual(libs, {i for i, _ in LIBS[lang]})
        self.assertFalse(pub(res)["publishable"])
        self.assertTrue([r for r in pub(res)["reasons"] if "groups" in r])
        R.validate(res)

    def test_the_shards_together_measure_what_one_run_measures(self):
        whole = go()
        parts = shard_set(3)
        self.assertEqual(names([c for p in parts for c in p["cells"]]), names(whole["cells"]))
        self.assertEqual(len([c for p in parts for c in p["cells"]]), len(whole["cells"]))

    def test_a_shard_that_names_a_missing_harness_is_an_error(self):
        with self.assertRaises(run.HarnessError):
            go(shard=(1, 9))


class Merge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parts = shard_set(3)
        cls.whole = go()

    def parts_copy(self):
        return copy.deepcopy(self.parts)

    def test_complete_shards_merge_into_a_publishable_result(self):
        self.assertTrue(pub(self.whole)["publishable"])
        merged = shards.merge(self.parts_copy())
        self.assertTrue(pub(merged)["publishable"], pub(merged)["reasons"])
        self.assertEqual(merged["run"]["selection"]["left_out"],
                         {"libraries": [], "tasks": [], "workloads": [], "groups": []})
        self.assertEqual(names(merged["cells"]), names(self.whole["cells"]))
        self.assertEqual(len(merged["run"]["shards"]), 3)
        R.validate(merged)

    def test_the_order_the_shards_are_given_in_does_not_matter(self):
        a = shards.merge(self.parts_copy())
        b = shards.merge(list(reversed(self.parts_copy())))
        self.assertEqual(a, b)

    def test_a_merged_result_renders_and_shows_every_shard(self):
        merged = shards.merge(self.parts_copy())
        with tempfile.TemporaryDirectory() as tmp:
            md = P.render(merged, Path(tmp))
        self.assertIn("**PUBLISHABLE.**", md)
        self.assertIn("Merged from 3 shards", md)
        self.assertIn("| 3 |", md)

    def test_a_shard_renders_with_its_place_in_the_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = P.render(copy.deepcopy(self.parts[1]), Path(tmp))
        self.assertIn("This is shard 2 of 3", md)

    def test_one_shard_alone_cannot_be_relabelled_publishable(self):
        p = self.parts_copy()[0]
        p["run"]["publishability"].update(publishable=True, label="publishable", reasons=[])
        with self.assertRaises(R.ResultError):
            R.validate(p)

    def test_a_missing_shard_is_refused(self):
        merge_fails(self, self.parts_copy()[:2], "shard 3 of 3 is missing")

    def test_a_missing_shard_allowed_by_flag_is_never_publishable(self):
        merged = shards.merge(self.parts_copy()[:2], allow_incomplete=True)
        self.assertFalse(pub(merged)["publishable"])
        self.assertTrue([r for r in pub(merged)["reasons"] if "left out" in r and "groups" in r])
        self.assertTrue(merged["run"]["selection"]["left_out"]["groups"])
        R.validate(merged)

    def test_a_duplicated_shard_is_refused(self):
        p = self.parts_copy()
        merge_fails(self, [p[0], p[1], p[1]], "shard 2 is given more than once")
        merge_fails(self, [p[0], p[1], p[1]], "shard 3 of 3 is missing")

    def test_plans_that_differ_are_refused(self):
        p = self.parts_copy()
        other = go(shard=(3, 3), tasks=["generic-load"])
        merge_fails(self, [p[0], p[1], other], "plan digests differ")

    def test_a_shard_that_claims_another_shards_rows_is_refused(self):
        p = self.parts_copy()
        p[0]["run"]["selection"]["groups"] = p[1]["run"]["selection"]["groups"]
        merge_fails(self, p, "does not hold the rows the plan gave it")

    def test_a_tampered_plan_is_refused(self):
        p = self.parts_copy()
        for part in p:
            g = part["run"]["shard"]["plan"]["groups"]
            g["go/w1"]["shard"] = 2 if g["go/w1"]["shard"] != 2 else 1
        merge_fails(self, p, "does not match its digest")

    def test_each_difference_in_what_ran_is_refused(self):
        cases = [
            ("kit revision", lambda r: r["run"]["environment"].update(kit_revision="def"), "different kit revisions"),
            ("mode", lambda r: r["run"].update(mode="quick"), "different modes"),
            ("cpu", lambda r: r["run"]["environment"].update(cpu="Other CPU"), "environment.cpu"),
            ("instance type", lambda r: r["run"]["environment"].update(instance_type="c7.xlarge"),
             "environment.instance_type"),
            ("instance type removed", lambda r: r["run"]["environment"].pop("instance_type"),
             "environment.instance_type"),
            ("os", lambda r: r["run"]["environment"].update(os="Linux 9.9"), "environment.os"),
            ("python", lambda r: r["run"]["environment"].update(python="3.12.1"), "environment.python"),
            ("counters", lambda r: r["run"].update(counters=True), "instructions"),
        ]
        for what, change, text in cases:
            with self.subTest(what):
                p = self.parts_copy()
                change(p[0])
                merge_fails(self, p, text)

    def test_other_inconsistencies_between_shards_are_refused(self):
        def stray_cell(p):
            other = next(g for g in p[1]["run"]["selection"]["groups"])
            lang, wl = other.split("/")
            extra = dict(cell("ours", "dump", wl), language=lang, role="own")
            extra.pop("type")
            p[0]["cells"].append(extra)

        def changed_input(p):
            for a in range(3):
                for b in range(a + 1, 3):
                    both = {w["id"] for w in p[a]["workloads"]} & {w["id"] for w in p[b]["workloads"]}
                    if both:
                        next(w for w in p[b]["workloads"] if w["id"] == min(both))["input_sha256"] = "00ff"
                        return
            self.fail("the fixture must give one workload to two shards")

        def memory_processes(p):
            p[0]["run"]["memory_processes"] = {"zz": {"declared": 5, "used": 5}}
            p[1]["run"]["memory_processes"] = {"zz": {"declared": 5, "used": 1}}

        for what, change, text in [("cells outside the rows", stray_cell, "cells outside its rows"),
                                   ("workload input", changed_input, "differs between shards (the input changed)"),
                                   ("memory processes", memory_processes, "different numbers of processes")]:
            with self.subTest(what):
                p = self.parts_copy()
                change(p)
                merge_fails(self, p, text)

    def test_a_language_measured_by_two_shards_must_have_the_same_harness_in_both(self):
        changes = [
            ("pin", lambda h: h["libraries"][0].update(version="2")),
            ("flags", lambda h: h["flags"].update(opt="3")),
            ("harness config", lambda h: h["config"].update(samples=99)),
            ("runtime", lambda h: h.update(runtime="rt2")),
        ]
        pairs = [(a, b) for a in range(3) for b in range(a + 1, 3)
                 if set(self.parts[a]["harnesses"]) & set(self.parts[b]["harnesses"])]
        self.assertTrue(pairs, "the fixture must give one language to two shards")
        x, y = pairs[0]
        shared = sorted(set(self.parts[x]["harnesses"]) & set(self.parts[y]["harnesses"]))
        for what, change in changes:
            with self.subTest(what):
                p = self.parts_copy()
                change(p[y]["harnesses"][shared[0]])
                merge_fails(self, p, f"the {shared[0]} harness differs between shards")

    def test_shards_that_ran_different_kit_files_are_refused_whatever_the_revision_says(self):
        # Same revision string on every machine, as with no git checkout or the same dirty commit.
        p = self.parts_copy()
        for x in p:
            x["run"]["environment"]["kit_revision"] = "abc"
        p[1]["run"]["kit_digest"] = "sha256:" + "0" * 64
        merge_fails(self, p, "kit digests differ")

    def test_a_shard_without_a_kit_digest_is_refused(self):
        p = self.parts_copy()
        del p[2]["run"]["kit_digest"]
        merge_fails(self, p, "does not record the digest")

    def test_a_revision_that_does_not_identify_the_code_is_not_publishable(self):
        for rev in (None, "abc+modified"):
            with self.subTest(rev):
                p = self.parts_copy()
                for x in p:
                    x["run"]["environment"]["kit_revision"] = rev
                    x["run"]["publishability"]["reasons"] = [
                        r for r in x["run"]["publishability"]["reasons"] if "kit" not in r]
                merged = shards.merge(p)
                self.assertFalse(pub(merged)["publishable"])
                self.assertTrue([r for r in pub(merged)["reasons"] if "kit" in r], pub(merged)["reasons"])

    def test_a_clean_packed_revision_with_matching_files_stays_publishable(self):
        p = self.parts_copy()
        for x in p:
            x["run"]["environment"]["kit_revision"] = "0123456789ab"
        self.assertTrue(pub(shards.merge(p))["publishable"])

    def test_the_run_itself_names_an_unidentified_revision(self):
        res = go(env=dict(ENV, kit_revision=None))
        self.assertFalse(pub(res)["publishable"])
        self.assertTrue([r for r in pub(res)["reasons"] if "kit revision is unknown" in r])
        self.assertTrue(res["run"]["kit_digest"].startswith("sha256:"))

    def test_a_cell_missing_from_a_held_row_makes_the_merge_not_publishable(self):
        p = self.parts_copy()
        p[0]["cells"] = [c for c in p[0]["cells"] if c["library"] != "weak"]
        merged = shards.merge(p)
        self.assertFalse(pub(merged)["publishable"])
        self.assertTrue([r for r in pub(merged)["reasons"] if r.startswith("shard 1:") and "have no cell" in r],
                        pub(merged)["reasons"])

    def test_a_crashed_cell_makes_the_merge_not_publishable_even_with_the_shard_reasons_scrubbed(self):
        p = self.parts_copy()
        c = p[0]["cells"][0]
        c.update(status="error", detail="died")
        for k in ("time", "memory", "variant"):
            c.pop(k, None)
        p[0]["run"]["publishability"]["reasons"] = []
        merged = shards.merge(p)
        self.assertFalse(pub(merged)["publishable"])
        self.assertTrue([r for r in pub(merged)["reasons"] if "did not finish" in r])

    def test_the_machines_may_differ_in_date_and_core(self):
        p = self.parts_copy()
        p[0]["run"]["environment"].update(date="2026-02-02T00:00:00Z", pinned_cpu=5)
        p[0]["run"]["publishability"]["measured"]["pinned_cpu"] = 5
        merged = shards.merge(p)
        self.assertTrue(pub(merged)["publishable"], pub(merged)["reasons"])
        self.assertEqual(merged["run"]["date"], "2026-01-01T00:00:00Z")

    def test_a_busy_shard_makes_the_merge_not_publishable_and_is_named(self):
        busy = [go(shard=(1, 3), load=9.0)] + [go(shard=(i, 3)) for i in (2, 3)]
        merged = shards.merge(busy)
        self.assertFalse(pub(merged)["publishable"])
        self.assertTrue([r for r in pub(merged)["reasons"] if r.startswith("shard 1:") and "load average" in r],
                        pub(merged)["reasons"])
        self.assertEqual(pub(merged)["measured"]["load1_start"], 9.0)

    def test_a_shard_on_the_wrong_governor_counts_even_if_its_own_reasons_were_scrubbed(self):
        p = self.parts_copy()
        p[2]["run"]["publishability"]["measured"]["governor"] = "powersave"
        merged = shards.merge(p)
        self.assertFalse(pub(merged)["publishable"])
        self.assertTrue([r for r in pub(merged)["reasons"] if r.startswith("shard 3:") and "governor" in r])
        self.assertEqual(pub(merged)["measured"]["governor"], "mixed")

    def test_an_unpinned_shard_or_a_crashed_cell_stays_against_the_merge(self):
        p = self.parts_copy()
        bad = next(c for c in p[1]["cells"])
        bad.update(status="error", detail="died")
        for k in ("time", "memory"):
            bad.pop(k)
        p[1]["run"]["publishability"]["reasons"].append("the run did not finish every cell it set out to measure")
        merged = shards.merge(p)
        self.assertFalse(pub(merged)["publishable"])
        self.assertTrue([r for r in pub(merged)["reasons"] if "did not finish" in r])

    def test_quick_shards_merge_but_stay_smoke(self):
        merged = shards.merge(shard_set(3, mode="quick"))
        self.assertFalse(pub(merged)["publishable"])
        self.assertTrue([r for r in pub(merged)["reasons"] if "quick run" in r])

    def test_filters_of_the_shards_carry_into_the_merge(self):
        merged = shards.merge([go(shard=(i, 2), libraries=["ours", "weak"], languages=["go"]) for i in (1, 2)])
        self.assertFalse(pub(merged)["publishable"])
        self.assertEqual(merged["run"]["selection"]["left_out"]["libraries"], ["go/rival"])
        self.assertEqual(merged["run"]["selection"]["libraries"], ["ours", "weak"])

    def test_a_result_that_is_not_a_shard_is_refused(self):
        merge_fails(self, [copy.deepcopy(self.whole)], "not a shard")


class Command(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parts = shard_set(2)

    def write_parts(self, tmp, parts):
        dirs = []
        for i, p in enumerate(parts):
            d = Path(tmp) / f"s{i}"
            run.write(p, d / "results.json")
            dirs.append(str(d))
        return dirs

    def merge(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["merge"] + args)
        return code, out.getvalue(), err.getvalue()

    def test_merge_writes_the_result_and_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirs = self.write_parts(tmp, self.parts)
            code, out, _ = self.merge(["--out", f"{tmp}/m"] + dirs)
            self.assertEqual(code, 0, out)
            res = json.loads((Path(tmp) / "m" / "results.json").read_text())
            R.validate(res)
            self.assertTrue((Path(tmp) / "m" / "report.md").is_file())
            self.assertIn("merged 2 shards", out)

    def test_a_refused_merge_exits_one_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirs = self.write_parts(tmp, self.parts[:1])
            code, _, err = self.merge(["--out", f"{tmp}/m"] + dirs)
            self.assertEqual(code, 1)
            self.assertIn("shard 2 of 2 is missing", err)
            self.assertFalse((Path(tmp) / "m").exists())

    def test_incomplete_flag_writes_a_smoke_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirs = self.write_parts(tmp, self.parts[:1])
            code, out, _ = self.merge(["--allow-incomplete", "--out", f"{tmp}/m"] + dirs)
            self.assertEqual(code, 0)
            self.assertIn("label: smoke", out)

    def test_plan_prints_the_plan_as_json(self):
        harnesses = {l: {"language": l, "_dir": Path(".")} for l in LIBS}
        out = io.StringIO()
        with mock.patch.object(run, "discover", return_value=harnesses), \
                mock.patch.object(shards, "offered_workloads", return_value=[dict(w) for w in OFFERED]), \
                redirect_stdout(out):
            code = cli.main(["plan", "--shards", "2"])
        self.assertEqual(code, 0)
        d = json.loads(out.getvalue())
        self.assertEqual(d["count"], 2)
        self.assertEqual(d["groups"], 6)
        self.assertEqual(sorted(g for s in d["shards"] for g in s["groups"]),
                         sorted(f"{l}/{w['id']}" for l in LIBS for w in OFFERED))
        self.assertTrue(d["plan_digest"].startswith("sha256:"))

    def test_a_bad_shard_argument_is_refused(self):
        for bad in ("0/3", "4/3", "x", "2"):
            with self.subTest(bad), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(["run", "--shard", bad])


if __name__ == "__main__":
    unittest.main()
