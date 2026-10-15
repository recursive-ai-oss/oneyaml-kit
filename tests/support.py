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


def run_adapters(names, cases, workers=1, slowest=()):
    """Build each adapter one after the other, then run them side by side: name -> result.

    The builds share a language's output folder and its restore cache, so they never overlap. A run
    only talks to a process of its own (every bounded case starts a fresh one), so the runs do not
    depend on each other, and the slowest adapter sets the time instead of the sum of all of them.
    The time-bound factor is never below 1, so running side by side cannot tighten a bound; the
    probe is a single-threaded loop, so it sees the extra load only when cores are saturated, and
    probes taken by two runs at the same moment must not stretch each other's bound (they share one
    interpreter lock, so the factor is measured under a lock);
    the memory bounds are measured per process and are not affected. Bounded cases
    can each grow a process by up to a gigabyte, and adapters reach them at the same point of the
    run, so `workers` is kept small to stay inside the memory the gate is given. The adapters named in
    `slowest` start first, so the longest run does not begin after the others have filled the workers.
    """
    from concurrent.futures import ThreadPoolExecutor

    from oneyaml_kit import runner
    manifests = {n: runner.load_manifest(n) for n in names}
    for n, m in manifests.items():
        # Same order as a single run: an adapter that cannot run here is reported as unavailable,
        # not as a failed build.
        why = runner.unavailable_reason(m, dict(os.environ))
        if why:
            raise runner.AdapterUnavailable(f"adapter {n}: not available: {why}")
    if workers > 1:
        # A start-up hook that sets an address-space limit runs between fork and exec, which is
        # not safe to do from several threads at once.
        limited = [n for n, m in manifests.items() if m.get("memory_limit_mb")]
        if limited:
            raise ValueError(f"adapters {limited} set memory_limit_mb, which starts them with a "
                             "pre-exec hook that is unsafe from several threads: run them with workers=1")
    for n, m in manifests.items():
        runner.build(m, dict(os.environ))
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(names)))) as ex:
        order = [n for n in slowest if n in names] + [n for n in names if n not in slowest]
        futures = {n: ex.submit(runner.run_adapter, n, cases, complete=True, skip_build=True) for n in order}
        return {n: futures[n].result() for n in names}


def _quote(s):
    """A double-quoted scalar that reads back as `s` under every reading the kit applies: printable
    characters are written as they are, the rest as escapes."""
    out = []
    for ch in s:
        o = ord(ch)
        if ch in '"\\':
            out.append("\\" + ch)
        elif ((0x20 <= o <= 0x7E or 0xA0 <= o <= 0xD7FF or 0xE000 <= o <= 0xFFFD or o >= 0x10000)
              and o not in (0x2028, 0x2029, 0xFEFF)):
            out.append(ch)
        elif o <= 0xFF:
            out.append("\\x%02x" % o)
        elif o <= 0xFFFF:
            out.append("\\u%04x" % o)
        else:
            out.append("\\U%08x" % o)
    return '"' + "".join(out) + '"'


def _float(f):
    t = repr(f)
    if "e" in t and "." not in t.split("e")[0]:
        m, x = t.split("e")
        t = m + ".0e" + x
    elif "." not in t and "e" not in t:
        t += ".0"
    return t


def write_safely(v):
    """YAML text for a kit value that is correct under the Core schema, YAML 1.1 and the common
    readers: JSON-style flow with every string double-quoted. The kit's tests use it as the
    writer that a correct library would be."""
    if v is None:
        return b"null\n"
    return (_node(v) + "\n").encode("utf-8")


def _node(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return _float(v)
    if isinstance(v, str):
        return _quote(v)
    if isinstance(v, list):
        return "[" + ", ".join(_node(x) for x in v) + "]"
    if set(v) == {"$float"}:
        return {"inf": ".inf", "-inf": "-.inf", "nan": ".nan"}[v["$float"]]
    if set(v) == {"$tagged"}:
        tagged = v["$tagged"]
        tag = "!!binary" if tagged["tag"] == "tag:yaml.org,2002:binary" else "!<%s>" % tagged["tag"]
        return tag + " " + _node(tagged["value"])
    if set(v) == {"$map"}:
        return "{" + ", ".join(f"? {_node(k)} : {_node(x)}" for k, x in v["$map"]) + "}"
    return "{" + ", ".join(f"{_quote(k)}: {_node(x)}" for k, x in v.items()) + "}"


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
        if k == "readback":
            return {"id": case_id, "status": "ok", "output": b64(write_safely(self.by_id[case_id].args["value"]))}
        if k == "bounded":
            a = e["accept"]
            if a is None:
                r = {"id": case_id, "status": "error", "error": {"category": e["category"][0]}}
                if op == "patch":
                    r["output"] = b64(data)  # a refused patch leaves the document as it was
                return r
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
    "events", "value", "dump", "patch:add", "patch:remove", "patch:replace", "patch:move",
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
