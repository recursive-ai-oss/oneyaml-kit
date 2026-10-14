package main

import (
	"math"
	"testing"
)

// read asks both parsers and fails the test unless they agree; it returns the common reading.
func read(t *testing.T, text string) any {
	t.Helper()
	v, e := twoReferees([]byte(text))
	if e != nil {
		t.Fatalf("%q: %v", text, e)
	}
	return v
}

func expect(t *testing.T, text, want string) {
	t.Helper()
	if d := firstDiff(read(t, text), mustJSON(t, want), ""); d != "" {
		t.Errorf("%q: %s", text, d)
	}
}

// The reference reading is the YAML 1.2 core schema, not either library's own typing.
func TestRefereesReadByTheCoreSchema(t *testing.T) {
	// Under the core schema `1_000` and `0b101` are text, `017` is the decimal 17, and a
	// leading-zero number that is not octal is still a decimal integer.
	expect(t, "b: 1_000\nc: 017\nd: 0b101\ne: 05509\n", `{"b": "1_000", "c": 17, "d": "0b101", "e": 5509}`)
	expect(t, "a: yes\nb: on\nc: y\nd: .5\ne: 0o17\nf: 0x1F\ng: 1e3\nh: ~\ni:\n",
		`{"a": "yes", "b": "on", "c": "y", "d": 0.5, "e": 15, "f": 31, "g": 1000.0, "h": null, "i": null}`)
	expect(t, "a: 'true'\nb: \"5\"\nc: !!str 7\nd: |\n  12\n", `{"a": "true", "b": "5", "c": "7", "d": "12\n"}`)
}

// The non-specific tag `!` makes a scalar a string. yaml.v3 does not report that tag, so the two
// parsers read `! 12` differently and the text cannot be judged: that is said, not guessed.
func TestRefereesDoNotGuessAtATagOneParserDropped(t *testing.T) {
	g, err := refereeGoccy([]byte("a: ! 12\n"))
	if err != nil || firstDiff(g, mustJSON(t, `{"a": "12"}`), "") != "" {
		t.Fatalf("goccy: %v %v", g, err)
	}
	if _, e := twoReferees([]byte("a: ! 12\n")); e == nil || e.rejected {
		t.Errorf("expected a disagreement, got %v", e)
	}
}

// A merge key is an ordinary key in YAML 1.2: nothing is merged.
func TestRefereesNeverMergeAndKeepAnchors(t *testing.T) {
	expect(t, "b:\n  <<:\n    k: 1\n  j: 2\n", `{"b": {"<<": {"k": 1}, "j": 2}}`)
	expect(t, "base: &b {x: 1}\nuse:\n  <<: *b\n  y: 2\n", `{"base": {"x": 1}, "use": {"<<": {"x": 1}, "y": 2}}`)
	expect(t, "- &a 1\n- *a\n", `[1, 1]`)
}

func TestRefereesReadSeveralDocuments(t *testing.T) {
	expect(t, "a: 1\n---\nb: 2\n", `[{"a": 1}, {"b": 2}]`)
	expect(t, "[1, {a: [x, y]}, '']\n", `[1, {"a": ["x", "y"]}, ""]`)
}

// A dump that writes a string as a plain scalar that the core schema reads as a number is
// wrong, and the guard says so; it does not give up on it.
func TestJudgeDumpOfALeadingZeroStringIsDiffersNotUnjudged(t *testing.T) {
	w := &Workload{ID: "zip", Input: []byte("zip: \"05509\"\n"), Documents: 1,
		Expected: []Named[any]{{"spec", mustJSON(t, `{"zip": "05509"}`)}}}
	if v := judge(Dump, w, bytesProduct([]byte("zip: 05509\n")), nil, nil, twoReferees); v.Status != "differs" {
		t.Fatalf("got %+v", v)
	}
	if v := judge(Dump, w, bytesProduct([]byte("zip: \"05509\"\n")), nil, nil, twoReferees); v.Status != "ok" {
		t.Errorf("got %+v", v)
	}
}

// One record of a library's dump of mixed scalar styles: a tab in a double-quoted scalar, a
// folded and a keep-chomped block scalar. The two parsers agree on the structure and the
// reading is the expected one.
func TestJudgeDumpOfMixedScalarStylesIsOk(t *testing.T) {
	text := "- double: \"tab\\there 0\"\n  folded: |\n    folded one 0 folded two\n  keep: |+\n    kept\n\n  plain: text 0\n  single: it's 0\n  strip: |-\n    stripped\n"
	want := `[{"double": "tab\there 0", "folded": "folded one 0 folded two\n", "keep": "kept\n\n", "plain": "text 0", "single": "it's 0", "strip": "stripped"}]`
	expect(t, text, want)
	w := &Workload{ID: "styles", Documents: 1, Expected: []Named[any]{{"spec", mustJSON(t, want)}}}
	if v := judge(Dump, w, bytesProduct([]byte(text)), nil, nil, twoReferees); v.Status != "ok" {
		t.Errorf("got %+v", v)
	}
}

func TestRefereesRejectWhatIsNotYAML(t *testing.T) {
	if _, e := twoReferees([]byte("a: [1, 2\n")); e == nil || !e.rejected {
		t.Errorf("got %v", e)
	}
}

func TestCoreResolverEdges(t *testing.T) {
	for in, want := range map[string]any{
		"+12": Int("12"), "-0": Int("0"), "007": Int("7"), "1.": 1.0, ".inf": math.Inf(1), "1_0": "1_0",
		"0b11": "0b11", "0o8": "0o8", "Yes": "Yes", "TRUE": true, "Null": nil, "1e": "1e",
		"12345678901234567890123": Int("12345678901234567890123"),
	} {
		if got := resolveCore(in); got != want {
			t.Errorf("%q: got %#v, want %#v", in, got, want)
		}
	}
}

// The lexer of one parser drops a tab inside a plain scalar; the reading takes the source text.
func TestRefereesKeepATabInsideAPlainScalar(t *testing.T) {
	expect(t, "a: tab\there\nb: x\t y\n", `{"a": "tab\there", "b": "x\t y"}`)
}
