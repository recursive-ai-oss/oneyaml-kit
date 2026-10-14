package main

import (
	"reflect"
	"strings"
	"testing"
	"time"
)

func mustJSON(t *testing.T, s string) any {
	t.Helper()
	v, err := fromJSON([]byte(s))
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func TestComparisonIsExactAboutTypes(t *testing.T) {
	want := mustJSON(t, `{"a": 1, "b": 1.0, "c": "1", "d": [true, null]}`)
	good := mustJSON(t, `{"d": [true, null], "c": "1", "b": 1.0, "a": 1}`)
	if d := firstDiff(good, want, ""); d != "" {
		t.Fatalf("equal values differ: %s", d)
	}
	for name, bad := range map[string]string{
		"integer for float": `{"a": 1, "b": 1, "c": "1", "d": [true, null]}`,
		"float for integer": `{"a": 1.0, "b": 1.0, "c": "1", "d": [true, null]}`,
		"number for string": `{"a": 1, "b": 1.0, "c": 1, "d": [true, null]}`,
		"missing key":       `{"a": 1, "b": 1.0, "c": "1"}`,
		"extra key":         `{"a": 1, "b": 1.0, "c": "1", "d": [true, null], "e": 0}`,
		"short list":        `{"a": 1, "b": 1.0, "c": "1", "d": [true]}`,
		"false for true":    `{"a": 1, "b": 1.0, "c": "1", "d": [false, null]}`,
	} {
		if firstDiff(mustJSON(t, bad), want, "") == "" {
			t.Errorf("%s was accepted", name)
		}
	}
}

func TestFromAnyConvertsWhatLibrariesReturn(t *testing.T) {
	got, err := fromAny(map[any]any{"a": int64(1), 80: uint64(2), true: 2.5,
		"t": time.Date(2001, 12, 14, 0, 0, 0, 0, time.UTC), "l": []any{nil, "x"}})
	if err != nil {
		t.Fatal(err)
	}
	want := mustJSON(t, `{"a": 1, "80": 2, "true": 2.5, "t": "timestamp 2001-12-14T00:00:00Z", "l": [null, "x"]}`)
	if d := firstDiff(got, want, ""); d != "" {
		t.Fatal(d)
	}
	if _, err := fromAny(struct{}{}); err == nil {
		t.Error("an unknown Go type was converted")
	}
}

func TestTypedConversionFollowsTagsAndSkipNone(t *testing.T) {
	geo := &Geo{Lat: 1, Lng: 2}
	note := "n"
	got, err := fromTyped(reflect.ValueOf([]Record{
		{ID: 7, Name: "x", Score: 2, Address: Address{City: "c", Geo: geo}, Note: &note},
		{ID: 8},
	}))
	if err != nil {
		t.Fatal(err)
	}
	l := got.([]any)
	first, second := l[0].(map[string]any), l[1].(map[string]any)
	if first["id"] != Int("7") || first["score"] != 2.0 || first["note"] != "n" {
		t.Errorf("first record: %v", first)
	}
	if v, present := second["note"]; !present || v != nil {
		t.Errorf("a nil pointer without skip-none must be null: %v", second["note"])
	}
	if _, present := second["address"].(map[string]any)["geo"]; present {
		t.Error("a nil skip-none pointer must leave the key out")
	}
	if tags, ok := second["tags"].([]any); !ok || len(tags) != 0 {
		t.Errorf("a nil slice must be an empty list: %v", second["tags"])
	}
	// A map field marked skip-none is left out when nil.
	step, err := fromTyped(reflect.ValueOf(Step{}))
	if err != nil {
		t.Fatal(err)
	}
	if _, present := step.(map[string]any)["with"]; present {
		t.Error("a nil skip-none map must leave the key out")
	}
}

func TestPointerSegmentsPickIndexOrKeyFromTheValue(t *testing.T) {
	base := mustJSON(t, `{"a/b": [{"c": 1}], "~": 2}`)
	got, err := pointerSegments(base, "/a~1b/0/c")
	if err != nil || !reflect.DeepEqual(got, []Seg{{Key: "a/b"}, {Index: 0, IsIdx: true}, {Key: "c"}}) {
		t.Fatalf("%v %v", got, err)
	}
	for _, p := range []string{"/nope", "/a~1b/5", "/~/x"} {
		if _, err := pointerSegments(base, p); err == nil {
			t.Errorf("%s resolved", p)
		}
	}
}

func TestBytesDiffNamesTheFirstDifference(t *testing.T) {
	if bytesDiff([]byte("abc"), []byte("abc")) != "" {
		t.Error("equal bytes differ")
	}
	if d := bytesDiff([]byte("abXd"), []byte("abcd")); !strings.HasPrefix(d, "byte 2:") {
		t.Error(d)
	}
	if bytesDiff([]byte("ab"), []byte("abc")) == "" {
		t.Error("a prefix is not equal")
	}
}

// ---- editing a tree ------------------------------------------------------------------------

type fnode struct {
	k     nodeKind
	text  string
	kids  []*fnode
	alias *fnode
}

func sc(s string) *fnode        { return &fnode{k: kScalar, text: s} }
func mp(kv ...*fnode) *fnode    { return &fnode{k: kMapping, kids: kv} }
func sq(items ...*fnode) *fnode { return &fnode{k: kSequence, kids: items} }

func render(n *fnode) string {
	switch n.k {
	case kScalar:
		return n.text
	case kAlias:
		return "*"
	}
	var p []string
	for _, c := range n.kids {
		p = append(p, render(c))
	}
	if n.k == kMapping {
		return "{" + strings.Join(p, " ") + "}"
	}
	return "[" + strings.Join(p, " ") + "]"
}

var fops = nodeOps[*fnode]{
	kind:       func(n *fnode) nodeKind { return n.k },
	content:    func(n *fnode) []*fnode { return n.kids },
	setContent: func(n *fnode, c []*fnode) { n.kids = c },
	text:       func(n *fnode) string { return n.text },
	target:     func(n *fnode) *fnode { return n.alias },
	setScalar:  func(n *fnode, v any) error { n.text = v.(string); return nil },
}

func TestNodeEditReplaceAndRemove(t *testing.T) {
	root := mp(sc("a"), sc("1"), sc("l"), sq(sc("x"), sc("y"), sc("z")), sc("m"), mp(sc("k"), sc("v")))
	steps := []EditStep{
		{Op: "replace", Segs: []Seg{{Key: "a"}}, Value: "2"},
		{Op: "remove", Segs: []Seg{{Key: "l"}, {Index: 1, IsIdx: true}}},
		{Op: "remove", Segs: []Seg{{Key: "m"}, {Key: "k"}}},
	}
	if err := fops.edit(root, steps); err != nil {
		t.Fatal(err)
	}
	if got := render(root); got != "{a 2 l [x z] m {}}" {
		t.Errorf("got %s", got)
	}
	if err := fops.edit(root, []EditStep{{Op: "remove", Segs: []Seg{{Key: "nope"}}}}); err == nil {
		t.Error("a missing key was removed")
	}
	if err := fops.edit(root, []EditStep{{Op: "frobnicate", Segs: []Seg{{Key: "a"}}}}); err == nil {
		t.Error("an unknown edit was applied")
	}
}

func TestNodeEditFollowsAliasesAndTakesTheFirstKey(t *testing.T) {
	anchored := mp(sc("k"), sc("old"))
	al := &fnode{k: kAlias, alias: anchored}
	root := mp(sc("a"), anchored, sc("b"), al, sc("d"), sc("1"), sc("d"), sc("2"))
	steps := []EditStep{
		{Op: "replace", Segs: []Seg{{Key: "b"}, {Key: "k"}}, Value: "new"},
		{Op: "replace", Segs: []Seg{{Key: "d"}}, Value: "first"},
	}
	if err := fops.edit(root, steps); err != nil {
		t.Fatal(err)
	}
	// Neither a write through an alias nor a repeated key is refused: the edit lands where a
	// user of the tree would put it, and the anchored node changes for every user.
	if render(anchored) != "{k new}" || render(root) != "{a {k new} b * d first d 2}" {
		t.Errorf("got %s", render(root))
	}
}

// ---- the loop adds nothing -----------------------------------------------------------------

func TestTheTimedLoopAddsNoAllocations(t *testing.T) {
	sink := []byte("x")
	r := newRunner(func() struct{} { return struct{}{} },
		func(*struct{}) ([]byte, error) { return sink, nil },
		bytesProduct)
	b := r.Prepare(1000)
	if n := testing.AllocsPerRun(5, b.Run); n != 0 {
		t.Errorf("the batch loop allocated %.0f times for 1000 iterations", n)
	}
}

func TestABatchRecoversAPanicInTheTask(t *testing.T) {
	r := newRunner(func() struct{} { return struct{}{} },
		func(*struct{}) ([]byte, error) { panic("boom") },
		bytesProduct)
	b := r.Prepare(3)
	b.Run()
	if b.Panic() == nil {
		t.Error("the panic was lost")
	}
}

// ---- the guard -----------------------------------------------------------------------------

func tinyWorkload(t *testing.T) *Workload {
	return &Workload{ID: "tiny", Input: []byte("a: 1\nb: [x, y]\n"), Documents: 1, Tasks: allTasks,
		Expected:     []Named[any]{{"spec", mustJSON(t, `{"a": 1, "b": ["x", "y"]}`)}},
		EditExpected: []Named[[]byte]{{"spec", []byte("a: 2\nb: [x, y]\n")}},
		PatchExpect:  []Named[[]byte]{{"spec", []byte("a: 2\nb: [x, y]\n")}}}
}

func TestRefereesAgreeOnPlainYAMLAndSayWhenTheyDoNot(t *testing.T) {
	v, e := twoReferees([]byte("a: [1, 2.5, x, null, true]\n"))
	if e != nil || firstDiff(v, mustJSON(t, `{"a": [1, 2.5, "x", null, true]}`), "") != "" {
		t.Fatalf("%v %v", v, e)
	}
	if _, e := twoReferees([]byte("a: [1, 2\n")); e == nil || !e.rejected {
		t.Errorf("unparseable text: %v", e)
	}
}

func TestJudgeDumpIsUnjudgedWhenTheRefereesDisagree(t *testing.T) {
	w := tinyWorkload(t)
	disagree := func([]byte) (any, *refereeError) { return nil, &refereeError{msg: "they disagree"} }
	if v := judge(Dump, w, bytesProduct([]byte("x")), nil, nil, disagree); v.Status != "unjudged" {
		t.Errorf("got %v", v)
	}
	none := func([]byte) (any, *refereeError) { return nil, &refereeError{rejected: true, msg: "no"} }
	if v := judge(Dump, w, bytesProduct([]byte("x")), nil, nil, none); v.Status != "differs" {
		t.Errorf("got %v", v)
	}
}

func TestValueOnlyIsAVariantNotAPass(t *testing.T) {
	w := tinyWorkload(t)
	failing := &FailedPatch{Errored: true, Bytes: w.Input}
	// The same value written in another style: not the expected bytes, the expected value.
	p := Product{Kind: BytesProduct, Bytes: []byte("a: 2\nb:\n- x\n- y\n"), ValueOnly: true}
	v := judge(PatchTask, w, p, nil, failing, twoReferees)
	if v.Status != "ok" || v.Variant != "value-only" {
		t.Fatalf("got %+v", v)
	}
	// A library that does not declare it cannot use it.
	p.ValueOnly = false
	if v := judge(PatchTask, w, p, nil, failing, twoReferees); v.Status != "differs" {
		t.Errorf("got %+v", v)
	}
	// A different value is wrong even for a library that cannot keep the text.
	p = Product{Kind: BytesProduct, Bytes: []byte("a: 3\nb: [x, y]\n"), ValueOnly: true}
	if v := judge(PatchTask, w, p, nil, failing, twoReferees); v.Status != "differs" {
		t.Errorf("got %+v", v)
	}
	// The failing patch must still fail and leave the text alone.
	p = Product{Kind: BytesProduct, Bytes: []byte("a: 2\nb: [x, y]\n")}
	if v := judge(PatchTask, w, p, nil, &FailedPatch{Errored: true, Bytes: []byte("changed")}, twoReferees); v.Status != "differs" {
		t.Errorf("a failed patch that changed the text: %+v", v)
	}
	if v := judge(PatchTask, w, p, nil, &FailedPatch{Errored: false, Bytes: w.Input}, twoReferees); v.Status != "differs" {
		t.Errorf("a failing patch that succeeded: %+v", v)
	}
}

func TestAgreementCheck(t *testing.T) {
	tm := Timing{AllocsPerOp: 100, BytesPerOp: 10000}
	if d := agree(tm, Memory{Allocations: 103, AllocatedBytes: 11500}); d != "" {
		t.Error(d)
	}
	if agree(tm, Memory{Allocations: 120, AllocatedBytes: 10000}) == "" {
		t.Error("allocation counts that differ by 20% were accepted")
	}
	if agree(tm, Memory{Allocations: 100, AllocatedBytes: 20000}) == "" {
		t.Error("bytes that differ by a factor of two were accepted")
	}
}
