"""The reader that judges written values, and the emit cases it judges."""

import unittest

import support
from oneyaml_kit import cases as C
from oneyaml_kit import compare as K
from oneyaml_kit import kitjson
from oneyaml_kit import readback as R
from oneyaml_kit.runner import execute

EMIT = [c for c in C.load_own() if c.category == "emit"]
CAPS = set(support.ALL_CAPS)


def read(text, schema="core"):
    return R.to_value(R.parse(text), R.SCHEMAS[schema])


def judge(text, value):
    return R.judge(text.encode("utf-8") if isinstance(text, str) else text, value)


def evaluate(value, out: bytes):
    """The verdicts K.evaluate gives for a dump of `value` that wrote `out`."""
    class Dump:
        op = "dump"
        args = {"value": value}
        profile = "core"

    r = K.evaluate(Dump, {"kind": "readback"}, {"status": "ok", "output": C.b64(out)}, CAPS)
    return r["loose"], r["strict"], r["detail"]


class Structure(unittest.TestCase):
    def test_block_and_flow_collections(self):
        self.assertEqual(read("a: 1\nb: [1, 2]\nc:\n  - x\n  - y: z\n    w: 1.5\n"),
                         {"a": 1, "b": [1, 2], "c": ["x", {"y": "z", "w": 1.5}]})
        self.assertEqual(read("- - a\n  - b\n- c\n"), [["a", "b"], "c"])
        self.assertEqual(read("a:\n- 1\n- 2\nb: 3\n"), {"a": [1, 2], "b": 3})
        self.assertEqual(read("{a: 1, 'b': [x, y], ? c : d, e}\n"), {"a": 1, "b": ["x", "y"], "c": "d", "e": None})
        self.assertEqual(read("[]\n"), [])
        self.assertEqual(read("{}\n"), {})

    def test_scalars_and_their_styles(self):
        self.assertEqual(read("a: plain\n  multi\n\n  line\n"), {"a": "plain multi\nline"})
        self.assertEqual(read("'it''s'\n"), "it's")
        self.assertEqual(read('"a\\tb\\u00e9 \\\n  cont"\n'), "a\tb\u00e9 cont")
        self.assertEqual(read('"a\n  b\n\n  c"\n'), "a b\nc")
        self.assertEqual(read("a: |\n  hi\n  there\nb: >-\n  one\n  two\n\n  three\n"),
                         {"a": "hi\nthere\n", "b": "one two\nthree"})
        self.assertEqual(read("a: |+\n  x\n\n\nb: 1\n"), {"a": "x\n\n\n", "b": 1})
        self.assertEqual(read("a: |2\n   x\n"), {"a": " x\n"})
        self.assertEqual(read("- |-\n  a\n   b\n- >\n  a\n   b\n  c\n"), ["a\n b", "a\n b\nc\n"])
        self.assertEqual(read("|+\n\n"), "\n")

    def test_explicit_and_complex_keys(self):
        self.assertEqual(read("? [1, 2]\n: x\n? |\n  blk\n: y\n"), {"$map": [[[1, 2], "x"], ["blk\n", "y"]]})
        self.assertEqual(read("1: a\ntrue: b\n"), {"$map": [[1, "a"], [True, "b"]]})

    def test_documents_comments_and_tags(self):
        self.assertIsNone(read(""))
        self.assertIsNone(read("# only a comment\n"))
        self.assertEqual(read("%YAML 1.2\n---\na: 1 # c\n...\n"), {"a": 1})
        self.assertEqual(read("--- |\n  x\n"), "x\n")
        self.assertEqual(read("\ufeffa: 1\n"), {"a": 1})
        self.assertEqual(read("x: !!str 0b-1\ny: !!float 1\nz: !<tag:yaml.org,2002:int> 5\n"),
                         {"x": "0b-1", "y": 1.0, "z": 5})

    def test_what_is_outside_the_reader_is_refused_by_name(self):
        bad = {
            "anchor": "&a x\n", "alias": "a: *b\n", "tag": "k: !foo x\n", "set": "!!set {a}\n",
            "two documents": "a: 1\n---\nb: 2\n", "directive": "%TAG ! tag:x,2000:\n---\na\n",
            "nested mapping on a line": "a: b: c\n", "multi-line key": "a: 1\n b: 2\n",
            "unterminated": "[1, 2\n", "unterminated quote": "a: 'x\n", "tab indent": "a:\n\t- b\n",
            "duplicate key": "a: 1\na: 2\n", "duplicate key by value": "1: a\n0x1: b\n",
            "bad escape": '"\\q"\n', "surrogate escape": '"\\ud800"\n', "wrong indent": "a:\n    b: 1\n   c: 2\n",
            "content after": "a\nb: c\n", "unindented flow": "a: [1,\n2]\n",
        }
        for name, text in bad.items():
            with self.assertRaises(R.ReadError, msg=name):
                read(text)

    def test_raw_non_printable_characters_are_refused(self):
        for ch in ("\x01", "\x7f", "\x9f", "\ufffe", "\uffff"):
            with self.assertRaises(R.ReadError, msg=hex(ord(ch))):
                read('"a%sb"\n' % ch)
        self.assertEqual(read('"a\u0085b"\n'), "a\u0085b")  # NEL is printable in YAML 1.2

    def test_depth_is_bounded_without_a_crash(self):
        with self.assertRaises(R.ReadError):
            read("[" * 500 + "]" * 500 + "\n")


class Resolution(unittest.TestCase):
    def test_core_schema(self):
        core = {"": None, "~": None, "null": None, "NULL": None, "true": True, "False": False,
                "0": 0, "-12": -12, "+5": 5, "0755": 755, "0o17": 15, "0x1F": 31,
                "1.5": 1.5, ".5": 0.5, "1.": 1.0, "1e3": 1000.0, "1E+17": 1e17, "-1.5e-3": -0.0015}
        for text, want in core.items():
            got = read("- " + text + "\n")[0] if text else read("- \n")[0]
            self.assertEqual((type(got), got), (type(want), want), text)
        self.assertEqual(read("[.inf, -.INF, .NaN]\n"), [{"$float": "inf"}, {"$float": "-inf"}, {"$float": "nan"}])
        for text in ("yes", "on", "y", "0b101", "0O7", "1_000", "1:30", "0b-1", "+-1", "Infinity", "NaN", ".",
                     "+", "0x", "1e", "0o8", "2001-12-14", "<<", "="):
            self.assertEqual(read("- " + text + "\n")[0], text, text)

    def test_yaml_1_1(self):
        y = {"yes": True, "No": False, "ON": True, "off": False, "y": True, "N": False,
             "0755": 493, "-0755": -493, "0b101": 5, "-0x1F": -31, "1_000": 1000, "1:30": 90, "190:20:30": 685230,
             "1.0e+17": 1e17, "1.5": 1.5, ".5": 0.5, "1:30.5": 90.5, "0": 0, "~": None}
        for text, want in y.items():
            got = read("- " + text + "\n", "yaml11")[0]
            self.assertEqual((type(got), got), (type(want), want), text)
        # Without a point and a signed exponent a float is a string to YAML 1.1.
        for text in ("1e17", "1E+17", "1e-5", "1.0e17", "08", "0o7"):
            self.assertEqual(read("- " + text + "\n", "yaml11")[0], text, text)
        for text in ("2001-12-14", "2001-12-14t21:59:43.10-05:00", "<<", "="):
            self.assertNotEqual(read("- " + text + "\n", "yaml11")[0], text, text)

    def test_forms_that_common_readers_take_as_numbers(self):
        for text in ("0b-1", "0o+7", "0b-1_0", "0B1", "0XFF", "0O7", "0X-F", "+-1", "--1", "-+1.5", "+_1", "1e3_",
                     "0_x123", "_1", "1_0.5"):
            self.assertTrue(R.quirk_number(text), text)
            self.assertNotEqual(read("- " + text + "\n", "common")[0], text, text)
        for text in ("0bad", "0b", "0x", "abc", "a_1", "1_a", "_", "__", "0b2", "0o8", "- 1", "1-1", "0b1x"):
            self.assertFalse(R.quirk_number(text), text)
            self.assertEqual(read("- '" + text + "'\n", "common")[0], text, text)


class AgreesWithTheSuite(unittest.TestCase):
    """The reader is checked against an independent oracle: the values that yaml-test-suite
    records for its valid documents, and the documents it records as invalid."""

    def test_valid_documents_read_as_the_suite_says(self):
        agree = differ = 0
        outside = []
        for c in C.load_yts():
            if not c.id.endswith("@value"):
                continue
            try:
                got = read(R.decode(c.input))
            except R.ReadError:
                outside.append(c.id)
                continue
            docs = c.expect["value"]
            if len(docs) == 1 and kitjson.values_equal(docs[0], got, ignore_tags=True):
                agree += 1
            elif not docs and got is None:
                agree += 1  # a stream with no document: the kit's reader calls it null
            else:
                differ += 1
        self.assertEqual(differ, 0)
        self.assertGreaterEqual(agree, 210)
        # What it does not read is anchors, custom tags, directives and several documents.
        self.assertLessEqual(len(outside), 70)

    def test_most_invalid_documents_are_refused(self):
        invalid = [c for c in C.load_yts() if c.expect["kind"] == "error"]
        self.assertEqual(len(invalid), 94)
        accepted = []
        for c in invalid:
            try:
                read(R.decode(c.input))
                accepted.append(c.id)
            except (R.ReadError, UnicodeDecodeError):
                pass
        # It is a judge of what a writer wrote, not a validator; this records how far it is one.
        self.assertLessEqual(len(accepted), 14, accepted)


class Judging(unittest.TestCase):
    def test_a_correct_output_passes_both(self):
        self.assertEqual(evaluate({"a": "yes", "b": [1, 2.5, None, True]},
                                  b"a: 'yes'\nb:\n  - 1\n  - 2.5\n  - null\n  - true\n")[:2], ("pass", "pass"))

    def test_a_wrongly_typed_read_back_fails_both(self):
        for value, text in (("1", b"1\n"), (1, b"'1'\n"), (1.0, b"1\n"), ("true", b"true\n"), ("", b"\n"),
                            ("0755", b"0755\n"), ({"a": 1}, b"a: 2\n"), ([1, 2], b"[2, 1]\n")):
            loose, strict, detail = evaluate(value, text)
            self.assertEqual((loose, strict), ("fail", "fail"), (value, text, detail))

    def test_a_document_that_does_not_parse_fails_both(self):
        for text in (b"a: [1\n", b"a: b: c\n", b"- a\n- [b\n", b"\xff\xfe"):
            loose, strict, detail = evaluate({"a": 1}, text)
            self.assertEqual((loose, strict), ("fail", "fail"), text)
            self.assertIn("does not read as YAML", detail)

    def test_an_implicit_key_of_1025_characters_fails_even_though_a_lenient_reader_takes_it(self):
        key = " " + "a" * 1022  # written with quotes this is 1025 characters
        value = {key: 1}
        loose, strict, detail = evaluate(value, ("'" + key + "': 1\n").encode())
        self.assertEqual((loose, strict), ("fail", "fail"))
        self.assertIn("1025", detail)
        for ok in (("? '" + key + "'\n: 1\n").encode(),
                   ("{'" + key + "': 1}\n").encode()):  # a flow mapping key is not limited to 1024
            self.assertEqual(evaluate(value, ok)[:2], ("pass", "pass"), ok)
        # 1024 characters including the quotes is the largest implicit key.
        key = " " + "a" * 1021
        self.assertEqual(evaluate({key: 1}, ("'" + key + "': 1\n").encode())[:2], ("pass", "pass"))
        # A multi-line implicit key never passes.
        self.assertEqual(evaluate({"a b": 1}, b'"a\n b": 1\n')[0], "fail")

    def test_a_1_1_only_problem_fails_strict_alone(self):
        for value, text in (("yes", b"yes\n"), ("on", b"on\n"), ("1_000", b"1_000\n"), ("1:30", b"1:30\n"),
                            ("0b101", b"0b101\n"), ("2001-12-14", b"2001-12-14\n"), ("<<", b"<<\n"),
                            (1e17, b"1E+17\n"), (1e-5, b"1e-05\n"), ("0b-1", b"0b-1\n"), ("0o+7", b"0o+7\n"),
                            ("0b-1_0", b"0b-1_0\n"), ("+-1", b"+-1\n"), ("1e3_", b"1e3_\n"), ("+_1", b"+_1\n"),
                            ("0XFF", b"0XFF\n"), ({"yes": 1}, b"yes: 1\n")):
            loose, strict, detail = evaluate(value, text)
            self.assertEqual((loose, strict), ("pass", "fail"), (value, text, detail))
        for value, text in ((1e17, b"1.0e+17\n"), (1e-5, b"1.0e-05\n"), ("yes", b"'yes'\n"), ("0b-1", b"'0b-1'\n")):
            self.assertEqual(evaluate(value, text)[:2], ("pass", "pass"), (value, text))

    def test_raw_line_separators_and_bom_fail_strict_alone(self):
        for ch in ("\u0085", "\u2028", "\u2029", "\ufeff"):
            loose, strict, detail = evaluate("a%sb" % ch, ('"a%sb"\n' % ch).encode())
            self.assertEqual((loose, strict), ("pass", "fail"), hex(ord(ch)))
            self.assertEqual(evaluate("a%sb" % ch, ('"a\\u%04xb"\n' % ord(ch)).encode())[:2], ("pass", "pass"))

    def test_raw_control_characters_fail_both(self):
        for ch in ("\x01", "\x7f", "\ufffe"):
            self.assertEqual(evaluate("a%sb" % ch, ('"a%sb"\n' % ch).encode())[:2], ("fail", "fail"), hex(ord(ch)))

    def test_a_raw_byte_order_mark_outside_quotes_fails_both(self):
        for text in ("- a\ufeffb\n", "- |\n  a\ufeffb\n", "- a # c\ufeffd\n", "a: \ufeffb\n"):
            loose, strict, detail = evaluate(["a\ufeffb"], text.encode())
            self.assertEqual((loose, strict), ("fail", "fail"), text)
        # Inside quotes it is a character of the scalar (strict objects, loose does not); at the
        # start of the stream it is the encoding signature.
        self.assertEqual(evaluate(["a\ufeffb"], '- "a\ufeffb"\n'.encode())[:2], ("pass", "fail"))
        self.assertEqual(evaluate(["a"], "\ufeff- a\n".encode())[:2], ("pass", "pass"))

    def test_leading_blank_lines_longer_than_the_first_text_line_need_an_indicator(self):
        for value, text in ((" \nabc\n", b"- |\n   \n  abc\n"), ("  \nfoo\n", b"- |\n    \n  foo\n"),
                            (" \nabc\n", b"k: |\n    \n  abc\n"), (" \nabc\n", b"- >\n   \n  abc\n")):
            if value.startswith(" ") and text.startswith(b"- >"):
                value = " \nabc\n"
            loose, strict, detail = evaluate([value] if text.startswith(b"- ") else {"k": value}, text)
            self.assertEqual((loose, strict), ("fail", "fail"), (text, detail))
            self.assertIn("indentation indicator", detail)
        self.assertEqual(evaluate([" \nabc\n"], b"- |2\n   \n  abc\n")[:2], ("pass", "pass"))
        self.assertEqual(evaluate([" \nabc\n"], b'- " \\nabc\\n"\n')[:2], ("pass", "pass"))
        # A leading blank line that is not longer than the text is fine.
        self.assertEqual(evaluate(["\nabc\n"], b"- |\n\n  abc\n")[:2], ("pass", "pass"))
        self.assertEqual(evaluate(["\nabc\n"], b"- |\n  \n  abc\n")[:2], ("pass", "pass"))

    def test_the_implicit_key_limit_counts_the_tag(self):
        key = "a" * 1020
        self.assertEqual(evaluate({key: 1}, ("!!str " + key + ": 1\n").encode())[0], "fail")  # 1026
        self.assertEqual(evaluate({key: 1}, ("? !!str " + key + "\n: 1\n").encode())[:2], ("pass", "pass"))
        key = "a" * 1018
        self.assertEqual(evaluate({key: 1}, ("!!str " + key + ": 1\n").encode())[:2], ("pass", "pass"))  # 1024
        self.assertEqual(evaluate({key: 1}, ("!!str " + key + " : 1\n").encode())[0], "fail")  # 1025, blank counts
        key = "a" * 1017
        self.assertEqual(evaluate({key: 1}, ("!!str " + key + " : 1\n").encode())[:2], ("pass", "pass"))  # 1024
        key = "a" * 1020
        self.assertEqual(evaluate([{key: 1}], ("- !!str " + key + ": 1\n").encode())[0], "fail")
        self.assertEqual(evaluate([{key: 1}], ("- {!!str " + key + ": 1}\n").encode())[:2], ("pass", "pass"))
        self.assertEqual(evaluate([{key: 1}], ("- [!!str " + key + ": 1]\n").encode())[0], "fail")

    def test_the_implicit_key_limit_counts_blanks_before_the_colon_in_later_keys(self):
        for n, want in ((1023, "pass"), (1024, "fail")):
            key = "a" * (n - 2)  # written with its quotes it is n characters
            self.assertEqual(evaluate({"x": 1, key: 2}, ("x: 1\n\"" + key + "\" : 2\n").encode())[0], want, n)

    def test_the_implicit_key_limit_counts_a_tag_on_a_later_block_key(self):
        for n, want in ((1024, "pass"), (1025, "fail")):
            key = "a" * (n - 6)  # written with "!!str " it is n characters
            self.assertEqual(evaluate({"x": 1, key: 2}, ("x: 1\n!!str " + key + ": 2\n").encode())[0], want, n)

    def test_a_byte_order_mark_may_start_a_line_of_a_document_prefix(self):
        bom = "\ufeff"
        for text in (bom + "- a\n", "# c\n" + bom + "- a\n", "%YAML 1.2\n" + bom + "---\n- a\n"):
            self.assertEqual(evaluate(["a"], text.encode())[:2], ("pass", "pass"), text)
        self.assertEqual(evaluate(["a"], ("- a\n...\n" + bom + "# end\n").encode())[:2], ("pass", "pass"))
        # After content, in the middle of a line, or twice in a row it is text and not allowed.
        for text in ("---\n" + bom + "- a\n", "- a" + bom + "\n", bom + bom + "- a\n", "- a\n" + bom + "# x\n"):
            self.assertEqual(evaluate(["a"], text.encode())[0], "fail", text)

    def test_one_or_two_digits_in_each_time_field_are_a_timestamp_in_yaml_1_1(self):
        for text in (b"2001-1-1 1:2:3\n", b"2001-12-14 1:02:03\n", b"2001-12-14t21:5:43.10-05:00\n"):
            self.assertEqual(evaluate(text.decode().strip(), text)[:2], ("pass", "fail"), text)

    def test_one_digit_month_and_day_are_a_timestamp_in_yaml_1_1(self):
        for text in (b"2001-1-1\n", b"2001-12-1\n", b"2001-1-12\n"):
            self.assertEqual(evaluate(text.decode().strip(), text)[:2], ("pass", "fail"), text)

    def test_unreadable_numbers_and_indicators_fail_cleanly(self):
        for text in ("- |\u00b2\n  a\n", "- |\u0661\n  a\n", "- " + "1" * 5000 + "\n", "- 0x" + "f" * 5000 + "\n"):
            loose, strict, detail = evaluate(["a\n"], text.encode())
            self.assertEqual((loose, strict), ("fail", "fail"), text[:20])
        self.assertEqual(evaluate([1], ("- " + "1" * 5000 + "\n").encode())[0], "fail")

    def test_an_empty_stream_is_no_document(self):
        for text in (b"", b"\n", b"# nothing\n", b"\xef\xbb\xbf"):
            loose, strict, detail = evaluate(None, text)
            self.assertEqual((loose, strict), ("fail", "fail"), text)
            self.assertIn("no document", detail)
        # An explicit empty document is the null value.
        self.assertEqual(evaluate(None, b"---\n")[:2], ("pass", "pass"))
        self.assertEqual(evaluate(None, b"null\n")[:2], ("pass", "pass"))

    def test_a_block_sequence_may_sit_at_the_indent_of_an_explicit_key_and_value(self):
        want = {"$map": [[["a", "b"], ["c"]]]}
        self.assertEqual(R.to_value(R.parse("?\n- a\n- b\n:\n- c\n"), R.SCHEMAS[R.CORE]), want)
        self.assertEqual(evaluate(want, b"?\n- a\n- b\n:\n- c\n")[:2], ("pass", "pass"))

    def test_an_error_or_missing_output_fails(self):
        class Dump:
            op = "dump"
            args = {"value": 1}
            profile = "core"

        for resp in ({"status": "error", "error": {"category": "other", "message": "no"}}, {"status": "ok"},
                     {"status": "accepted"}, {"status": "ok", "output": "!!!"}):
            r = K.evaluate(Dump, {"kind": "readback"}, resp, CAPS)
            self.assertEqual((r["loose"], r["strict"]), ("fail", "fail"), resp)
        r = K.evaluate(Dump, {"kind": "readback"}, {"status": "unsupported", "reason": "x"}, CAPS)
        self.assertEqual((r["loose"], r["strict"]), ("unsupported", "unsupported"))

    def test_the_failure_detail_quotes_what_was_written(self):
        _, _, detail = evaluate({"k": "1"}, b"k: 1\n")
        self.assertIn("k: 1", detail)
        self.assertIn("$.k", detail)


def notes_of(case):
    path = support.ROOT / "suites" / "own" / "cases" / case.id.split(":", 1)[1] / "case.json"
    return kitjson.loads(path.read_bytes()).get("notes", "")


class EmitCases(unittest.TestCase):
    def test_the_cases_are_the_ones_the_family_rule_needs(self):
        ids = {c.id for c in EMIT}
        for name in ("implicit-key-written-length", "radix-prefix-with-inner-sign-is-quoted",
                     "float-without-a-point-keeps-a-point-in-exponent-form",
                     "yaml-1-1-boolean-words-are-quoted", "leading-zero-integer-strings-are-quoted",
                     "number-preceded-by-two-signs-is-quoted", "number-with-underscores-is-quoted",
                     "sixty-four-bit-edge-integers", "empty-sequence-document", "empty-mapping-document",
                     "multi-line-string-reads-back", "line-separators-and-bom-are-escaped"):
            self.assertIn("own:emit/" + name, ids)
        self.assertGreaterEqual(len(EMIT), 25)
        for c in EMIT:
            self.assertEqual((c.op, c.expect, c.input), ("dump", {"kind": "readback"}, b""), c.id)
            self.assertNotIn("convention", c.tags, c.id)
            # The family convention is the strict layer only; the notes say so for the cases it marks.
            self.assertEqual("strict-convention" in c.tags, "writing convention of the family" in notes_of(c), c.id)
            self.assertIn(c.origin, ("bug", "spec"), c.id)
            self.assertTrue(c.refs or c.provenance, c.id)

    def test_the_three_reported_bugs_are_cases_with_the_reported_values(self):
        by = {c.id: c.args["value"] for c in EMIT}
        keys = list(by["own:emit/implicit-key-written-length"])
        self.assertIn(" " + "a" * 1022, keys)
        self.assertIn("'" * 600, keys)
        self.assertEqual(by["own:emit/radix-prefix-with-inner-sign-is-quoted"], ["0b-1", "0o+7", "0b-1_0"])
        floats = by["own:emit/float-without-a-point-keeps-a-point-in-exponent-form"]
        self.assertEqual(floats[:3], [1e17, 1e-5, 5e-324])
        self.assertTrue(all(type(f) is float for f in floats))

    def test_no_case_holds_the_text_that_stands_for_a_misread_scalar(self):
        def strings(v):
            todo = [v]
            while todo:
                x = todo.pop()
                if isinstance(x, str):
                    yield x
                elif isinstance(x, list):
                    todo.extend(x)
                elif isinstance(x, dict):
                    todo.extend(x.keys())
                    todo.extend(x.values())

        marks = set(R._MARKERS.values())
        for c in EMIT:
            self.assertFalse(marks & set(strings(c.args["value"])), c.id)

    def test_a_correct_writer_passes_every_case(self):
        oracle = support.OracleAdapter(EMIT)
        for r in execute(oracle, CAPS, EMIT):
            self.assertEqual((r["loose"], r["strict"]), ("pass", "pass"), (r["id"], r.get("detail")))

    def test_a_writer_that_writes_every_string_plain_fails_the_quoting_cases(self):
        def naive(v):
            if isinstance(v, str):
                return v
            if isinstance(v, list):
                return "[" + ", ".join(naive(x) for x in v) + "]"
            if isinstance(v, dict):
                return "{" + ", ".join(f"{k}: {naive(x)}" for k, x in v.items()) + "}"
            return repr(v)

        picked = [c for c in EMIT if "quoted" in c.id or "escaped" in c.id]
        self.assertGreaterEqual(len(picked), 15)
        survivors = [c.id for c in picked
                     if evaluate(c.args["value"], naive(c.args["value"]).encode("utf-8"))[1] == "pass"]
        self.assertEqual(survivors, [])

    def test_cases_are_not_sent_to_an_adapter_without_the_capability(self):
        oracle = support.OracleAdapter(EMIT)
        res = execute(oracle, {"events", "value"}, EMIT)
        self.assertEqual(oracle.sent, [])
        self.assertTrue(all(r["loose"] == "unsupported" and r["reason"] == "capability" for r in res))

    def test_a_dump_case_is_checked_when_it_loads(self):
        import json
        import shutil
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        try:
            def make(name, op, expect, extra=None):
                d = tmp / "emit" / name
                d.mkdir(parents=True)
                case = {"format": "oneyaml-kit-case/2", "id": f"emit/{name}", "title": "t", "category": "emit",
                        "origin": "spec", "refs": ["x"], "operation": op, "expect": expect}
                case.update(extra or {})
                (d / "case.json").write_text(json.dumps(case))
                return d

            good = make("ok", {"type": "dump", "value": [1]}, {"kind": "readback"})
            self.assertEqual(C.load_own_case(good, tmp).args["value"], [1])
            bad = [
                ({"type": "dump"}, {"kind": "readback"}, None),
                ({"type": "dump", "value": [1]}, {"kind": "value", "value": [[1]]}, None),
                ({"type": "dump", "value": [1]}, {"kind": "error", "category": "any"}, None),
                ({"type": "value"}, {"kind": "readback"}, None),
                ({"type": "dump", "value": [1]}, {"kind": "readback", "file": "x"}, None),
                ({"type": "dump", "value": [1]}, {"kind": "readback"}, {"input": "input.yaml"}),
                ({"type": "dump", "value": [1]}, {"kind": "readback"}, {"input_recipe": [{"text": "a"}]}),
                ({"type": "dump", "value": float("nan")}, {"kind": "readback"}, None),
                ({"type": "dump", "value": {"$bad": 1}}, {"kind": "readback"}, None),
                ({"type": "dump", "value": 1, "extra": 2}, {"kind": "readback"}, None),
            ]
            for i, (op, expect, extra) in enumerate(bad):
                d = make(f"bad{i}", op, expect, extra)
                if extra and "input" in extra:
                    (d / "input.yaml").write_bytes(b"a")
                with self.assertRaises(C.CaseError, msg=repr((op, expect, extra))):
                    C.load_own_case(d, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
