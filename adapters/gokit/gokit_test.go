package gokit

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"strings"
	"testing"
	"time"
)

func doc(t *testing.T, v any) string {
	t.Helper()
	b, err := Documents([]any{v})
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestIntegersAndFloatsStayApart(t *testing.T) {
	cases := map[string]any{
		`[1]`:                    int64(1),
		`[1.0]`:                  float64(1),
		`[1e+300]`:               1e300,
		`[-0.5]`:                 -0.5,
		`[12345678901234567890]`: uint64(12345678901234567890),
	}
	for want, v := range cases {
		if got := doc(t, v); got != want {
			t.Errorf("%T %v: got %s, want %s", v, v, got, want)
		}
	}
}

func TestNonFiniteFloatsUseTheEscapeForm(t *testing.T) {
	for want, f := range map[string]float64{
		`[{"$float":"inf"}]`: math.Inf(1), `[{"$float":"-inf"}]`: math.Inf(-1), `[{"$float":"nan"}]`: math.NaN(),
	} {
		if got := doc(t, f); got != want {
			t.Errorf("got %s, want %s", got, want)
		}
	}
}

func TestMappingsWithReservedOrNonStringKeysUseTheMapForm(t *testing.T) {
	if got := doc(t, map[string]any{"b": 1, "a": "x"}); got != `[{"a":"x","b":1}]` {
		t.Errorf("plain object: %s", got)
	}
	if got := doc(t, map[string]any{"$float": "inf"}); got != `[{"$map":[["$float","inf"]]}]` {
		t.Errorf("reserved key: %s", got)
	}
	if got := doc(t, map[any]any{true: 1}); got != `[{"$map":[[true,1]]}]` {
		t.Errorf("non-string key: %s", got)
	}
	if got := doc(t, map[any]any{"k": nil}); got != `[{"k":null}]` {
		t.Errorf("string keys: %s", got)
	}
}

func TestUnknownGoTypesAreRefusedNotGuessed(t *testing.T) {
	if _, err := Documents([]any{struct{}{}}); err == nil {
		t.Error("a struct was written")
	}
}

func TestTimestampsAreTextAndBytesAreTheirString(t *testing.T) {
	ts := time.Date(2001, 12, 14, 0, 0, 0, 0, time.UTC)
	if got := doc(t, ts); got != `["2001-12-14T00:00:00Z"]` {
		t.Errorf("timestamp: %s", got)
	}
	if got := doc(t, []byte("ab")); got != `["ab"]` {
		t.Errorf("bytes: %s", got)
	}
}

func TestStringsKeepHTMLCharacters(t *testing.T) {
	if got := doc(t, "<&>"); got != `["<&>"]` {
		t.Errorf("got %s", got)
	}
}

func TestFromJSONKeepsNumberText(t *testing.T) {
	v, err := FromJSON([]byte(`{"a":[1,1.0,2e3,12345678901234567890123]}`))
	if err != nil {
		t.Fatal(err)
	}
	if got := doc(t, v); got != `[{"a":[1,1.0,2000.0,12345678901234567890123]}]` {
		t.Errorf("got %s", got)
	}
}

func TestPointer(t *testing.T) {
	good := map[string][]string{"": nil, "/": {""}, "/a/b": {"a", "b"}, "/a~1b/~0": {"a/b", "~"}, "/~01": {"~1"}}
	for in, want := range good {
		got, err := Pointer(in)
		if err != nil || strings.Join(got, "\x00") != strings.Join(want, "\x00") || len(got) != len(want) {
			t.Errorf("%q: got %q, %v; want %q", in, got, err, want)
		}
	}
	if _, err := Pointer("a/b"); err == nil {
		t.Error("a pointer without a leading slash was accepted")
	}
}

func TestPatchValueTypesNumbersByTheirText(t *testing.T) {
	v, err := PatchValue(json.RawMessage(`[1, 1.5, 2.0, {"k": 3}]`))
	if err != nil {
		t.Fatal(err)
	}
	l := v.([]any)
	if _, ok := l[0].(int64); !ok {
		t.Errorf("1 is %T", l[0])
	}
	if _, ok := l[2].(float64); !ok {
		t.Errorf("2.0 is %T", l[2])
	}
	if _, ok := l[3].(OMap)[0].Value.(int64); !ok {
		t.Errorf("nested 3 is %T", l[3])
	}
}

// A small tree stands in for a library's nodes, so the pointer logic is tested without one.
type node struct {
	kind     Kind
	text     string
	children []*node
	target   *node
}

func scalar(s string) *node { return &node{kind: Scalar, text: s} }
func mapping(kv ...*node) *node {
	return &node{kind: Mapping, children: kv}
}
func seq(items ...*node) *node { return &node{kind: Sequence, children: items} }

var fake = Tree[*node]{
	Kind:       func(n *node) Kind { return n.kind },
	Content:    func(n *node) []*node { return n.children },
	SetContent: func(n *node, c []*node) { n.children = c },
	Text:       func(n *node) string { return n.text },
	Target:     func(n *node) *node { return n.target },
	Set: func(dst *node, v any) error {
		*dst = node{kind: Scalar, text: v.(string)}
		return nil
	},
	New: func(v any) (*node, error) { return scalar(v.(string)), nil },
}

func show(n *node) string {
	switch n.kind {
	case Scalar:
		return n.text
	case Alias:
		return "*" + n.target.text
	}
	var parts []string
	for _, c := range n.children {
		parts = append(parts, show(c))
	}
	if n.kind == Mapping {
		return "{" + strings.Join(parts, " ") + "}"
	}
	return "[" + strings.Join(parts, " ") + "]"
}

func apply(root *node, ops ...Op) (*Failure, string) { return fake.Apply(root, ops) }

func op(name, path string, v any) Op {
	return Op{Name: name, Path: path, HasPath: true, Value: v, HasValue: v != nil}
}

func TestApplyReplaceRemoveAdd(t *testing.T) {
	root := mapping(scalar("a"), scalar("1"), scalar("l"), seq(scalar("x"), scalar("y")))
	if f, u := apply(root, op("replace", "/a", "2"), op("remove", "/l/0", nil), op("add", "/l/-", "z"), op("add", "/k", "v")); f != nil || u != "" {
		t.Fatal(f, u)
	}
	if got := show(root); got != "{a 2 l [y z] k v}" {
		t.Errorf("got %s", got)
	}
	if f, _ := apply(root, op("add", "/l/0", "first")); f != nil || show(root) != "{a 2 l [first y z] k v}" {
		t.Errorf("insert: %v %s", f, show(root))
	}
}

func TestApplyFailures(t *testing.T) {
	mk := func() *node {
		return mapping(scalar("80"), scalar("1"), scalar("80"), scalar("2"), scalar("l"), seq(scalar("x")))
	}
	for _, c := range []struct {
		op   Op
		want string
	}{
		{op("replace", "/nope", "9"), "patch-path"},
		{op("remove", "/l/5", nil), "patch-path"},
		{op("remove", "/l/01", nil), "patch-path"},
		{op("add", "/l/9", "q"), "patch-path"},
		{op("replace", "l/0", "q"), "patch-invalid"},
		{op("frobnicate", "/l", "q"), "patch-invalid"},
		{Op{Name: "replace"}, "patch-invalid"},
		{op("replace", "/l/0/deeper", "q"), "patch-path"},
	} {
		f, u := apply(mk(), c.op)
		if u != "" || f == nil || f.Category != c.want {
			t.Errorf("%v: got %v %q, want %s", c.op, f, u, c.want)
		}
	}
}

func TestApplyTakesTheFirstMatchingKeyAndNeverRefuses(t *testing.T) {
	root := mapping(scalar("80"), scalar("1"), scalar("80"), scalar("2"))
	if f, _ := apply(root, op("replace", "/80", "9")); f != nil {
		t.Fatal(f)
	}
	if got := show(root); got != "{80 9 80 2}" {
		t.Errorf("got %s", got)
	}
}

func TestPatchObjectsKeepTheirKeyOrder(t *testing.T) {
	v, err := PatchValue(json.RawMessage(`{"z":1,"a":{"y":[{"k":true},2.5],"b":null}}`))
	if err != nil {
		t.Fatal(err)
	}
	keys := func(m OMap) string {
		var s []string
		for _, p := range m {
			s = append(s, p.Key)
		}
		return strings.Join(s, ",")
	}
	m := v.(OMap)
	if keys(m) != "z,a" || keys(m[1].Value.(OMap)) != "y,b" {
		t.Errorf("order lost: %v", v)
	}
	n, _ := Build(v, func(x any) (*node, error) { return scalar(fmt.Sprint(x)), nil },
		func(c []*node) *node { return &node{kind: Mapping, children: c} },
		func(c []*node) *node { return &node{kind: Sequence, children: c} })
	if got := show(n); got != "{z 1 a {y [{k true} 2.5] b <nil>}}" {
		t.Errorf("built %s", got)
	}
	if _, ok := Plain(v).(map[string]any); !ok {
		t.Error("Plain left an OMap")
	}
}

func TestApplyDeclinesWhatItCannotDo(t *testing.T) {
	for _, o := range []Op{op("test", "/a", "1"), op("move", "/a", nil), op("replace", "", "x")} {
		if _, u := apply(mapping(scalar("a"), scalar("1")), o); u == "" {
			t.Errorf("%v was not declined", o)
		}
	}
}

func TestApplyFollowsAnAliasToTheAnchoredNode(t *testing.T) {
	anchored := mapping(scalar("k"), scalar("old"))
	root := mapping(scalar("a"), anchored, scalar("b"), &node{kind: Alias, target: anchored})
	if f, _ := apply(root, op("replace", "/b/k", "new")); f != nil {
		t.Fatal(f)
	}
	// The write lands in the shared node, so every place that uses it changes. Refusing the
	// write is a judgement for the case to make, not for this code.
	if show(anchored) != "{k new}" {
		t.Errorf("anchored node is %s", show(anchored))
	}
}

func TestAcceptedHasNoOtherFields(t *testing.T) {
	if got := string(Accepted().encode("x")); got != "{\"id\":\"x\",\"status\":\"accepted\"}\n" {
		t.Errorf("got %s", got)
	}
}

func TestResultEncoding(t *testing.T) {
	b := Err("syntax", "bad \"x\"", 2, 3).WithOutput([]byte("hi")).WithEmulated("unchanged").encode("id1")
	var m map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(b), &m); err != nil {
		t.Fatal(err, string(b))
	}
	e := m["error"].(map[string]any)
	if m["id"] != "id1" || m["status"] != "error" || e["category"] != "syntax" || e["line"] != float64(2) || e["column"] != float64(3) {
		t.Errorf("%s", b)
	}
	if m["output"] != "aGk=" || m["emulated"].([]any)[0] != "unchanged" {
		t.Errorf("%s", b)
	}
	// Position is left out when the library gives none, never written as zero.
	if bytes.Contains(Err("other", "m", 0, 0).encode("x"), []byte("line")) {
		t.Error("zero position written")
	}
	if !bytes.Contains(Unsupported("why").encode("x"), []byte(`"reason":"why"`)) {
		t.Error("reason missing")
	}
	if !bytes.Contains(OKEvents([]string{}).encode("x"), []byte(`"events":[]`)) {
		t.Error("an empty event list must stay a list")
	}
}

func TestNativeKeepsIntegersFloatsAndNestingApart(t *testing.T) {
	v, err := Native([]byte(`[1, 1.0, 1e3, 12345678901234567890, {"a": [null, "x"]}, {"$float": "-inf"}]`))
	if err != nil {
		t.Fatal(err)
	}
	l := v.([]any)
	if _, ok := l[0].(int64); !ok {
		t.Errorf("1 is %T", l[0])
	}
	if _, ok := l[1].(float64); !ok {
		t.Errorf("1.0 is %T", l[1])
	}
	if _, ok := l[2].(float64); !ok {
		t.Errorf("1e3 is %T", l[2])
	}
	if _, ok := l[3].(uint64); !ok {
		t.Errorf("a 64-bit unsigned is %T", l[3])
	}
	if m := l[4].(map[string]any); m["a"].([]any)[0] != nil || m["a"].([]any)[1] != "x" {
		t.Errorf("nested: %v", m)
	}
	if !math.IsInf(l[5].(float64), -1) {
		t.Errorf("-inf is %v", l[5])
	}
}

func TestNativeDeclinesWhatAdaptersCannotWrite(t *testing.T) {
	for _, raw := range []string{``, `{"$map": [[1, 2]]}`, `[100000000000000000000]`, `[`} {
		if v, err := Native([]byte(raw)); err == nil {
			t.Errorf("%q: got %v, want an error", raw, v)
		}
	}
}

func TestByteStringsGoToTheLibrariesHookOrAreDeclined(t *testing.T) {
	raw := `[{"$tagged": {"tag": "tag:yaml.org,2002:binary", "value": "//4AAYA="}}]`
	v, err := NativeWith([]byte(raw), InvalidUTF8String)
	if err != nil {
		t.Fatal(err)
	}
	if got := v.([]any)[0]; got != "\xff\xfe\x00\x01\x80" {
		t.Errorf("got %q", got)
	}
	// No hook: declined, never written as text or numbers.
	if _, err := Native([]byte(raw)); err == nil {
		t.Error("a byte string was accepted without a hook")
	}
	// Bytes that are valid UTF-8 would be written as text by the string route.
	if _, err := NativeWith([]byte(`{"$tagged": {"tag": "tag:yaml.org,2002:binary", "value": "aGVsbG8="}}`), InvalidUTF8String); err == nil {
		t.Error("valid UTF-8 bytes were handed to the string route")
	}
	// Any other tag is not something the adapters write.
	if _, err := NativeWith([]byte(`{"$tagged": {"tag": "!color", "value": "red"}}`), InvalidUTF8String); err == nil {
		t.Error("a custom tag was accepted")
	}
	// The adapter's reason reaches the answer.
	a := &Adapter{NoBinary: "bytes are written as a list", Dump: func(any) Result { return Unsupported("not reached") }}
	if _, err := NativeWith([]byte(raw), a.binary()); err == nil || err.Error() != "bytes are written as a list" {
		t.Errorf("reason: %v", err)
	}
}
