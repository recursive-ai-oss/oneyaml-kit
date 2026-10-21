"""The Python adapters: their shared helper, pins and honest declarations.

The static checks and the helper's own tests always run. Building the adapters (needs uv and
network access to the package index) and asking each for its hello only happens when
KIT_ADAPTER_TESTS=1 (the gate sets it).
"""

import io
import json
import os
import re
import subprocess
import sys
import unittest

import support
from oneyaml_kit import runner, vocabulary

ADAPTERS = support.ROOT / "adapters"
sys.path.insert(0, str(ADAPTERS / "pykit"))
import pykit  # noqa: E402

# Built from the pinned libraries; the checkout-linked ones are optional.
LIBRARIES = {
    "pyyaml-c": ("6.0.3", {"events", "error-events", "value", "dump", "error-category", "error-position"}),
    "pyyaml-pure": ("6.0.3", {"events", "error-events", "value", "dump", "error-category", "error-position"}),
    "ruamel-safe": ("0.19.1", {"events", "error-events", "value", "dump", "error-category", "error-position"}),
    "ruamel-rt": ("0.19.1", {"events", "error-events", "value", "dump", "error-category", "error-position",
                             "patch:add", "patch:remove", "patch:replace", "patch:move", "patch:copy",
                             "patch:test", "patch:merge"}),
    "yamlrocks": ("0.7.1", {"outcome:events", "value", "dump", "error-category", "error-position",
                            "patch:add", "patch:remove", "patch:replace"}),
    "yaml-rs": ("0.1.6", {"outcome:events", "value", "dump", "error-category"}),
    "py-yaml12": ("0.2.0", {"outcome:events", "value", "dump", "error-category"}),
    "ryaml": ("0.5.1", {"outcome:events", "value", "dump", "error-category"}),
}
LINKED = ("oneyaml-python", "oneyaml-python-pure")


class Registration(unittest.TestCase):
    def test_the_expected_adapters_exist(self):
        found = sorted(p.name for p in ADAPTERS.iterdir()
                       if (p / "adapter.json").is_file() and runner.load_manifest(p.name)["language"] == "python")
        self.assertEqual(found, sorted([*LIBRARIES, *LINKED]))

    def test_every_manifest_is_complete(self):
        for name in [*LIBRARIES, *LINKED]:
            m = runner.load_manifest(name)
            self.assertEqual(m["name"], name)
            self.assertTrue((ADAPTERS / name / m["deviations"]).is_file(), name)
            self.assertTrue((ADAPTERS / name / "main.py").is_file(), name)
            # Two processes can be alive at once (the shared one and a bounded case's own), and
            # the whole run has a few gigabytes.
            self.assertLessEqual(m["memory_limit_mb"], 2048, name)
        for name in LINKED:
            self.assertEqual(runner.load_manifest(name)["requires_env"], ["KIT_ONEYAML_PYTHON_SRC"])
        for name in LIBRARIES:
            self.assertNotIn("requires_env", runner.load_manifest(name))

    def test_libraries_are_pinned_exactly_with_hashes(self):
        pins = {}
        for line in (ADAPTERS / "pykit" / "libs.in").read_text().splitlines():
            if line and not line.startswith("#"):
                name, _, version = line.partition("==")
                pins[name.lower().replace("_", "-")] = version
        self.assertEqual(pins, {"pyyaml": "6.0.3", "ruamel.yaml": "0.19.1", "yamlrocks": "0.7.1",
                                "yaml-rs": "0.1.6", "py-yaml12": "0.2.0", "ryaml": "0.5.1",
                                "jsonpatch": "1.36", "json-merge-patch": "0.3.0"})
        for lock in ("libs.lock", "tools.lock"):
            text = (ADAPTERS / "pykit" / lock).read_text()
            blocks = re.split(r"\n(?=\S)", text)
            pinned = [b for b in blocks if re.match(r"[A-Za-z0-9_.\-]+==", b)]
            self.assertTrue(pinned, lock)
            for b in pinned:
                self.assertIn("--hash=sha256:", b, f"{lock}: {b.splitlines()[0]}")

    def test_the_build_installs_binary_wheels_only_and_checks_the_hashes(self):
        for script in ("build.sh", "build-oneyaml.sh"):
            text = (ADAPTERS / "pykit" / script).read_text()
            self.assertIn("--require-hashes", text, script)
            self.assertIn("--only-binary :all:", text, script)


class HelperValues(unittest.TestCase):
    def test_value_notation(self):
        docs = [{"a": [1, 2.5, True, None, "x"], "$k": 1}, {1: "int key"}, [float("nan"), float("-inf")]]
        got = json.loads(pykit.documents_json(docs))
        self.assertEqual(got, [{"$map": [["a", [1, 2.5, True, None, "x"]], ["$k", 1]]},
                               {"$map": [[1, "int key"]]},
                               [{"$float": "nan"}, {"$float": "-inf"}]])
        self.assertIn("2.5", pykit.documents_json([[2.5]]))
        self.assertEqual(pykit.documents_json([[1.0, 1]]), "[[1.0,1]]")

    def test_a_deep_value_needs_no_recursion(self):
        deep = cur = []
        for _ in range(200_000):
            nxt = []
            cur.append(nxt)
            cur = nxt
        text = pykit.documents_json([deep])
        self.assertEqual(text.count("["), 200_002)  # the documents list and 200001 levels

    def test_a_cycle_is_an_error_the_adapter_raises(self):
        a = []
        a.append(a)
        with self.assertRaises(pykit.Failure) as cm:
            pykit.documents_json([a])
        self.assertIn("error", cm.exception.emulated)

    def test_a_shared_graph_that_would_expand_without_end_is_accepted_not_described(self):
        node = ["x"]
        for _ in range(30):
            node = [node, node]
        with self.assertRaises(pykit.Accepted):
            pykit.documents_json([node])

    def test_a_shared_node_below_the_budget_is_written_each_time(self):
        shared = {"k": 1}
        self.assertEqual(json.loads(pykit.documents_json([[shared, shared]])), [[{"k": 1}, {"k": 1}]])

    def test_tagged_set_and_bytes(self):
        out = json.loads(pykit.documents_json([[pykit.Tagged("!t", {"a": 1}), {"s"}, b"hi"]]))
        self.assertEqual(out, [[{"$tagged": {"tag": "!t", "value": {"a": 1}}}, {"s": None}, "hi"]])

    def test_a_type_with_no_notation_is_declined(self):
        with self.assertRaises(pykit.Declined):
            pykit.documents_json([object()])

    def test_the_request_side(self):
        got = pykit.from_kit({"$map": [[1, {"$float": "inf"}], ["b", [{"x": None}]]]})
        self.assertEqual(got, {1: float("inf"), "b": [{"x": None}]})
        self.assertEqual(pykit.from_kit({"$tagged": {"tag": pykit.BINARY_TAG, "value": "aGk="}}, binary=bytes), b"hi")
        with self.assertRaises(pykit.Declined):
            pykit.from_kit({"$tagged": {"tag": pykit.BINARY_TAG, "value": "aGk="}})
        with self.assertRaises(pykit.Declined):  # 1, 1.0 and true are one key in a Python dict
            pykit.from_kit({"$map": [[1, "a"], [True, "b"]]})
        with self.assertRaises(pykit.Declined):  # a list cannot be a key
            pykit.from_kit({"$map": [[[1], "a"]]})

    def test_a_deep_request_needs_no_recursion(self):
        v = "leaf"
        for _ in range(100_000):
            v = [v]
        out = pykit.from_kit(v)
        n = 0
        while isinstance(out, list):
            out = out[0]
            n += 1
        self.assertEqual(n, 100_000)


class HelperText(unittest.TestCase):
    def test_encodings_are_detected_as_the_specification_says(self):
        for enc, bom in (("utf-8", b"\xef\xbb\xbf"), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff"),
                         ("utf-32-le", b"\xff\xfe\x00\x00"), ("utf-32-be", b"\x00\x00\xfe\xff")):
            data = bom + "a: é\n".encode(enc)
            self.assertEqual(pykit.decode_text(data), ("a: é\n", True), enc)
        for enc in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
            self.assertEqual(pykit.decode_text("a: 1\n".encode(enc)), ("a: 1\n", True), enc)
        self.assertEqual(pykit.decode_text("a: é\n".encode()), ("a: é\n", False))

    def test_malformed_input_is_an_encoding_error_at_the_byte(self):
        with self.assertRaises(pykit.Failure) as cm:
            pykit.decode_text(b"ab\n\xffcd")
        f = cm.exception
        self.assertEqual((f.category, f.line, f.column), ("encoding", 2, 1))
        self.assertEqual(sorted(f.emulated), ["decode", "error"])
        with self.assertRaises(pykit.Failure) as cm:
            pykit.decode_text(b"\xef\xbb\xbfa\xc3")  # the mark is not counted
        self.assertEqual((cm.exception.line, cm.exception.column), (1, 2))

    def test_positions_count_all_three_line_breaks(self):
        self.assertEqual(pykit.line_column("a\r\nb\rc\n"), (4, 1))
        self.assertEqual(pykit.line_column("ab"), (1, 3))


def make(name, **kw):
    return type(name, (), kw)()


class HelperEvents(unittest.TestCase):
    def test_lines(self):
        tag = lambda ev: getattr(ev, "tag", None)  # noqa: E731
        cases = [
            (make("StreamStartEvent"), "+STR"),
            (make("DocumentStartEvent", explicit=True), "+DOC ---"),
            (make("DocumentEndEvent", explicit=False), "-DOC"),
            (make("MappingStartEvent", anchor="a", tag="tag:x,2000:t", flow_style=True), "+MAP {} &a <tag:x,2000:t>"),
            (make("SequenceStartEvent", anchor=None, tag=None, flow_style=False), "+SEQ"),
            (make("AliasEvent", anchor="a"), "=ALI *a"),
            (make("ScalarEvent", anchor=None, tag=None, style=None, value="a\tb\\\n"), "=VAL :a\\tb\\\\\\n"),
            (make("ScalarEvent", anchor=None, tag=None, style='"', value=""), '=VAL "'),
        ]
        for ev, want in cases:
            self.assertEqual(pykit.event_line(ev, tag), want)


class Loop(unittest.TestCase):
    def converse(self, handlers, classify, *requests):
        lines = [json.dumps(r) for r in requests]
        out = io.BytesIO()
        pykit.serve({"name": "t", "version": "1", "language": "python", "capabilities": [], "emulates": []},
                    handlers, classify, stdin=io.BytesIO(("\n".join(lines) + "\n").encode()), stdout=out)
        return [json.loads(x) for x in out.getvalue().splitlines()]

    def test_the_protocol(self):
        class Own(Exception):
            pass

        def boom(data, args):
            raise Own("bad input")

        def bug(data, args):
            raise KeyError("an adapter bug")

        def declined(data, args):
            raise pykit.Declined("no")

        def refused(data, args):
            raise pykit.Failure("patch-path", "m", 1, 2, events=["+STR"], output=b"in", emulated=("unchanged",))

        def value(data, args):
            return pykit.ok(value=pykit.JSONText('[{"a":1}]'), emulated=["pointer"])

        classify = lambda e: pykit.Failure("syntax", str(e), 3, 4) if isinstance(e, Own) else None  # noqa: E731
        got = self.converse(
            {"events": boom, "value": value, "patch": refused, "dump": declined, "x": bug}, classify,
            {"type": "hello", "protocol": 2},
            {"type": "run", "id": "1", "op": "events", "input": ""},
            {"type": "run", "id": "2", "op": "value", "input": ""},
            {"type": "run", "id": "3", "op": "patch", "input": "", "args": {}},
            {"type": "run", "id": "4", "op": "dump", "input": ""},
            {"type": "run", "id": "5", "op": "x", "input": ""},
            {"type": "run", "id": "6", "op": "unknown", "input": ""},
            {"type": "bye"},
        )
        self.assertEqual(got[0]["type"], "hello")
        self.assertEqual(got[0]["protocol"], 2)
        by = {r["id"]: r for r in got[1:]}
        self.assertEqual(by["1"], {"id": "1", "status": "error",
                                   "error": {"category": "syntax", "message": "bad input", "line": 3, "column": 4}})
        self.assertEqual(by["2"], {"id": "2", "status": "ok", "value": [{"a": 1}], "emulated": ["pointer"]})
        self.assertEqual(by["3"]["error"], {"category": "patch-path", "message": "m", "line": 1, "column": 2})
        self.assertEqual((by["3"]["events"], by["3"]["output"], by["3"]["emulated"]),
                         (["+STR"], "aW4=", ["unchanged"]))
        self.assertEqual(by["4"]["status"], "unsupported")
        # An exception the adapter does not recognise is a crash, never a rejection of the input.
        self.assertEqual(by["5"]["status"], "crash")
        self.assertEqual(by["6"]["status"], "unsupported")

    def test_a_request_json_cannot_read_still_gets_an_answer(self):
        deep = '{"type":"run","id":"d1","op":"dump","input":"","args":{"value":' + "[" * 100_000 + "]" * 100_000 + "}}"
        out = io.BytesIO()
        pykit.serve({}, {}, None, stdin=io.BytesIO(deep.encode() + b"\n"), stdout=out)
        self.assertEqual(json.loads(out.getvalue())["status"], "unsupported")


class Built(unittest.TestCase):
    """Builds each library adapter and checks what it says in its hello."""

    @classmethod
    def setUpClass(cls):
        if os.environ.get("KIT_ADAPTER_TESTS") != "1":
            raise unittest.SkipTest("set KIT_ADAPTER_TESTS=1 to build the Python adapters")

    def hello(self, name):
        m = runner.load_manifest(name)
        env = dict(os.environ)
        runner.build(m, env)
        r = subprocess.run(m["command"], cwd=m["_dir"], env=env, capture_output=True, text=True,
                           input='{"type":"hello","protocol":2}\n{"type":"bye"}\n', timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        return json.loads(r.stdout.splitlines()[0])

    def test_every_hello_is_honest_and_pinned(self):
        for name, (version, caps) in LIBRARIES.items():
            h = self.hello(name)
            self.assertEqual((h["name"], h["version"], h["protocol"]), (name, version, 2), name)
            self.assertEqual(set(h["capabilities"]), caps, name)
            self.assertEqual(vocabulary.check(h["emulates"]), [], name)
            self.assertEqual(h["info"]["python"].split(".")[:2], ["3", "13"], name)

    def test_the_c_loader_adapter_really_uses_libyaml(self):
        h = self.hello("pyyaml-c")
        self.assertTrue(h["info"]["libyaml"])
        self.assertEqual(h["info"]["loader"], "CSafeLoader")
        self.assertEqual(self.hello("pyyaml-pure")["info"]["loader"], "SafeLoader")


if __name__ == "__main__":
    unittest.main()
