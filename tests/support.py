"""Shared helpers for the kit's own tests."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.dont_write_bytecode = True

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
                return {"id": case_id, "status": "error", "error": {"category": e["category"]}}
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
