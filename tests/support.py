"""Shared helpers for the kit's own tests."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.dont_write_bytecode = True
# The kit's own tests pin the time-bound factor so that their timings do not depend on the machine.
# The tests that run real adapters keep the measured factor: they are the ones that must hold on a
# slow or busy machine.
if os.environ.get("KIT_ADAPTER_TESTS") != "1":
    os.environ.setdefault("KIT_TIME_SCALE", "1")

from oneyaml_kit.cases import b64  # noqa: E402


class OracleAdapter:
    """An in-process adapter that answers every case the way its expectation says a
    correct library would. The kit's own tests use it to prove that every case can be
    passed, and that changing an expectation makes the same answers fail."""

    def __init__(self, cases):
        self.by_id = {c.id: c for c in cases}
        self.sent = []

    def run(self, case_id, op, data, args, limits=None):
        r = self._answer(case_id, op, data, args)
        if limits:
            r["_measure"] = {"seconds": 0.0, "peak_mb": 0.0}
        return r

    def _answer(self, case_id, op, data, args):
        self.sent.append(case_id)
        e = self.by_id[case_id].expect
        k = e["kind"]
        if k == "events":
            return {"id": case_id, "status": "ok", "events": e["events"]}
        if k == "value":
            return {"id": case_id, "status": "ok", "value": e["value"]}
        if k == "bytes":
            return {"id": case_id, "status": "ok", "output": b64(e["bytes"])}
        if k == "bounded":
            a = e["accept"]
            if a is None:
                return {"id": case_id, "status": "error", "error": {"category": e["category"][0]}}
            if a["kind"] == "events":
                return {"id": case_id, "status": "ok", "events": a["events"]}
            if a["kind"] == "value":
                return {"id": case_id, "status": "ok", "value": a["value"]}
            return {"id": case_id, "status": "ok", "output": b64(a["bytes"])}
        err = {"category": e["category"] if e["category"] != "any" else "syntax", "message": "x"}
        if e["line"] is not None:
            err["line"] = e["line"]
            if e["column"] is not None:
                err["column"] = e["column"]
        r = {"id": case_id, "status": "error", "error": err, "output": b64(data)}
        if e["events"] is not None:
            r["events"] = e["events"]
        return r


ALL_CAPS = {
    "events", "value", "patch:add", "patch:remove", "patch:replace", "patch:move",
    "patch:copy", "patch:test", "patch:merge", "error-category", "error-position", "error-events",
}


# Inputs for the `duplicate-key` category an adapter maps from its library. The first two are
# refused by every library that refuses repeated keys; the rest are errors or documents that must
# not take the category, including text that looks like a library's own duplicate-key message.
DUPLICATE_KEY_DOCS = (b"a: 1\na: 2\n", b"{b: x, b: y}\n")
NOT_DUPLICATE_KEY_ERRORS = (b"a: [1\n", b"a: b: c\n", b"a: [Duplicate key a\n", b"*nope\n",
                            b'a: ["mapping key \\"a\\" already defined at line 1"\n')
NOT_ERRORS = (b'{a: "mapping key \\"a\\" already defined at [1:2]", b: "Duplicate key a"}\n',
              b"a: \"Duplicate mapping key 'a'.\"\nb: 1\n")


def check_duplicate_key_category(test, name):
    """Starts the built adapter `name` and checks that it names repeated keys `duplicate-key`, and
    nothing else."""
    from oneyaml_kit.protocol import SubprocessAdapter
    from oneyaml_kit.runner import load_manifest
    m = load_manifest(name)
    ad = SubprocessAdapter(m["command"], cwd=m["_dir"], timeout=30)
    ad.start()
    try:
        for i, doc in enumerate(DUPLICATE_KEY_DOCS):
            r = ad.run("dup%d" % i, "value", doc, {})
            test.assertEqual((r["status"], r["error"]["category"]), ("error", "duplicate-key"), (name, doc))
        for i, doc in enumerate(NOT_DUPLICATE_KEY_ERRORS):
            r = ad.run("bad%d" % i, "value", doc, {})
            test.assertEqual(r["status"], "error", (name, doc))
            test.assertNotEqual(r["error"]["category"], "duplicate-key", (name, doc))
        for i, doc in enumerate(NOT_ERRORS):
            r = ad.run("ok%d" % i, "value", doc, {})
            test.assertNotEqual(r["status"], "error", (name, doc))
    finally:
        ad.close()
