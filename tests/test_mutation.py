"""The kit bites: altering an expectation, or removing a case, makes it fail."""

import copy
import unittest

import support
from oneyaml_kit import cases as C
from oneyaml_kit import compare as K
from oneyaml_kit.runner import execute

ALL = C.load_all(include_slow=True)


def run(cases, answers_from=None):
    """Run `cases` against an oracle that answers from `answers_from` (default: themselves)."""
    oracle = support.OracleAdapter(answers_from or cases)
    return execute(oracle, set(support.ALL_CAPS), cases), oracle


def mutate(expect):
    """A different expectation of the same kind: what a careless edit would produce."""
    e = dict(expect)  # a copy of the top level is enough: the lists below are replaced, not edited
    k = e["kind"]
    if k == "events":
        e["events"] = e["events"] + ["=VAL :zzz"]
    elif k == "value":
        e["value"] = e["value"] + [None]
    elif k == "bytes":
        # A byte at the front: the first difference is then at byte 0, so a result of tens of
        # megabytes is judged at once instead of after a pass over all of it.
        e["bytes"] = b" " + e["bytes"]
    return e


class OracleSanity(unittest.TestCase):
    def test_every_case_can_be_passed(self):
        res, _ = run(ALL)
        bad = [(r["id"], r["loose"], r["strict"], r.get("detail")) for r in res
               if r["loose"] != "pass" or r["strict"] != "pass"]
        self.assertEqual(bad, [])
        self.assertGreater(len(res), 600)


class Mutations(unittest.TestCase):
    def mutated_results(self, cases, fn):
        out = []
        for c in cases:
            m = copy.copy(c)
            m.expect = fn(c.expect)
            res, _ = run([m], answers_from=[c])
            out.append((c.id, res[0]))
        return out

    def test_changing_a_success_expectation_fails_every_case(self):
        ok = [c for c in ALL if c.expect["kind"] in ("events", "value", "bytes")]
        self.assertGreater(len(ok), 500)
        survivors = [cid for cid, r in self.mutated_results(ok, mutate)
                     if r["loose"] == "pass" or r["strict"] == "pass"]
        self.assertEqual(survivors, [])

    def test_turning_a_failure_expectation_into_a_success_fails_every_case(self):
        errs = [c for c in ALL if c.expect["kind"] == "error"]
        self.assertGreater(len(errs), 90)
        survivors = [cid for cid, r in self.mutated_results(
            errs, lambda e: {"kind": "events", "events": ["+STR", "-STR"]}) if r["loose"] == "pass"]
        self.assertEqual(survivors, [])

    def test_changing_a_stated_error_detail_fails_strict(self):
        stated = 0
        for c in ALL:
            e = c.expect
            if e["kind"] != "error":
                continue
            variants = []
            if e["category"] != "any":
                variants.append({**e, "category": "limit" if e["category"] != "limit" else "syntax"})
            if e["line"] is not None:
                variants.append({**e, "line": e["line"] + 1})
            if e["column"] is not None:
                variants.append({**e, "column": e["column"] + 1})
            if e["events"] is not None:
                variants.append({**e, "events": e["events"] + ["=VAL :zzz"]})
            for v in variants:
                stated += 1
                m = copy.copy(c)
                m.expect = v
                res, _ = run([m], answers_from=[c])
                self.assertEqual(res[0]["strict"], "fail", (c.id, v))
                self.assertEqual(res[0]["loose"], "pass", (c.id, v))
        self.assertGreater(stated, 90)

    def test_changing_a_bounded_expectation_fails(self):
        bounded = [c for c in ALL if c.expect["kind"] == "bounded"]
        self.assertGreaterEqual(len(bounded), 2)
        for c in bounded:
            e = c.expect
            variants = []
            if e["accept"] is not None:
                a = mutate(e["accept"])
                variants.append(({**e, "accept": a}, ("fail", "fail")))
            if e["category"] is not None and e["accept"] is None:
                # The oracle answers with the first listed category. A mutation must leave
                # that one out, whatever the list holds: replacing it by `syntax` changed
                # nothing for a case that already expects syntax first.
                cats = list(e["category"])
                other = next(x for x in ("limit", "syntax", "encoding") if x not in cats)
                variants.append(({**e, "category": [other]}, ("pass", "fail")))
                if len(cats) > 1:
                    variants.append(({**e, "category": cats[1:]}, ("pass", "fail")))
                variants.append(({**e, "accept": None, "category": None, "error": None}, None))
            for v, want in variants:
                if want is None:
                    continue
                m = copy.copy(c)
                m.expect = v
                res, _ = run([m], answers_from=[c])
                self.assertEqual((res[0]["loose"], res[0]["strict"]), want, (c.id, v))

    def test_a_wrong_category_is_caught_on_bounded_cases_that_also_accept_a_result(self):
        # The oracle answers such a case with its result, so the category is never used. Answer
        # with a refusal in the first listed category instead: it passes strict, and a planted
        # category that leaves it out fails strict (loose accepts any error).
        both = [c for c in ALL if c.expect["kind"] == "bounded"
                and c.expect["accept"] is not None and c.expect["category"] is not None]
        self.assertGreaterEqual(len(both), 10)
        caps = set(support.ALL_CAPS)
        for c in both:
            cats = list(c.expect["category"])
            refusal = {"id": c.id, "status": "error", "error": {"category": cats[0]},
                       "_measure": {"seconds": 0.0, "peak_mb": 0.0}}
            if c.expect.get("unchanged"):
                # A refused patch that must leave the document alone reports the document as it was.
                refusal["output"] = C.b64(c.input)
            r = K.evaluate(c, c.expect, refusal, caps)
            self.assertEqual((r["loose"], r["strict"]), ("pass", "pass"), c.id)
            other = next(x for x in ("limit", "syntax", "encoding") if x not in cats)
            planted = [{**c.expect, "category": [other]}]
            if len(cats) > 1:
                planted.append({**c.expect, "category": cats[1:]})
            for e in planted:
                r = K.evaluate(c, e, refusal, caps)
                self.assertEqual((r["loose"], r["strict"]), ("pass", "fail"), (c.id, e["category"]))
                self.assertIn("category", r["detail"], c.id)

    def test_a_bounded_refusal_that_changes_the_document_is_caught(self):
        stated = [c for c in ALL if c.expect["kind"] == "bounded" and c.expect["unchanged"]]
        self.assertGreaterEqual(len(stated), 3)
        caps = set(support.ALL_CAPS)
        m = {"seconds": 0.0, "peak_mb": 0.0}
        for c in stated:
            refusal = {"id": c.id, "status": "error", "error": {"category": c.expect["category"][0]},
                       "_measure": m}
            r = K.evaluate(c, c.expect, dict(refusal, output=C.b64(c.input)), caps)
            self.assertEqual((r["loose"], r["strict"]), ("pass", "pass"), c.id)
            for output in (C.b64(c.input + b" "), C.b64(c.input[:-1]), None):
                resp = dict(refusal) if output is None else dict(refusal, output=output)
                r = K.evaluate(c, c.expect, resp, caps)
                self.assertEqual((r["loose"], r["strict"]), ("pass", "fail"), c.id)

    def test_changing_the_value_of_a_dump_case_fails_it(self):
        # A dump case holds its value in the operation: the same output must stop passing when the
        # value changes, whatever the value is.
        dumps = [c for c in ALL if c.op == "dump"]
        self.assertGreaterEqual(len(dumps), 25)
        for c in dumps:
            for changed in ([c.args["value"]], {"k": c.args["value"]}):
                m = copy.copy(c)
                m.args = {**c.args, "value": changed}
                res, _ = run([m], answers_from=[c])
                self.assertEqual((res[0]["loose"], res[0]["strict"]), ("fail", "fail"), c.id)

    def test_unchanged_is_enforced(self):
        refusals = [c for c in ALL if c.expect["kind"] == "error" and c.expect["unchanged"]]
        self.assertGreaterEqual(len(refusals), 5)
        for c in refusals:
            resp = {"status": "error", "error": {"category": c.expect["category"]},
                    "output": C.b64(c.input + b" ")}
            r = K.evaluate(c, c.expect, resp, set(support.ALL_CAPS))
            self.assertEqual((r["loose"], r["strict"]), ("pass", "fail"), c.id)

    def test_corrected_expectation_is_judged_separately(self):
        c = next(x for x in ALL if x.id == "yts:T833")
        resp = {"id": c.id, "status": "error", "error": {"category": "syntax"},
                "events": c.correction["expect"]["events"]}

        class A:
            def run(self, *a):
                return resp

        res = execute(A(), set(support.ALL_CAPS), [c])[0]
        self.assertEqual((res["loose"], res["strict"]), ("pass", "fail"))
        self.assertEqual((res["corrected"]["loose"], res["corrected"]["strict"]), ("pass", "pass"))

    def test_an_adapter_that_is_wrong_everywhere_fails_everything(self):
        class Wrong:
            def run(self, cid, op, data, args, limits=None):
                return {"id": cid, "status": "ok", "events": ["+STR"], "value": ["nope"], "output": C.b64(b"nope")}

        res = execute(Wrong(), set(support.ALL_CAPS), ALL)
        self.assertEqual([r["id"] for r in res if r["loose"] == "pass" and r["strict"] == "pass"], [])


class Unsupported(unittest.TestCase):
    def test_cases_needing_undeclared_capabilities_are_not_sent_and_not_failed(self):
        cases = [c for c in ALL if c.suite == "own" and c.op == "patch"]
        oracle = support.OracleAdapter(cases)
        res = execute(oracle, {"events", "value"}, cases)
        self.assertEqual(oracle.sent, [])
        self.assertTrue(all(r["loose"] == "unsupported" and r["reason"] == "capability" for r in res))

    def test_partial_capability_runs_only_what_it_declares(self):
        cases = [c for c in ALL if c.suite == "own" and c.op == "patch"]
        oracle = support.OracleAdapter(cases)
        res = execute(oracle, {"patch:replace"}, cases)
        sent = set(oracle.sent)
        for r in res:
            c = next(x for x in cases if x.id == r["id"])
            needs = K.required_caps(c, c.expect)[0]
            self.assertEqual(r["id"] in sent, K.satisfied(needs, {"patch:replace"}), r["id"])


if __name__ == "__main__":
    unittest.main()
