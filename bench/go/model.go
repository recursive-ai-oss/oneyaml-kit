package main

// The contract between the harness and a library: tasks, workloads, and the small
// interfaces a library implements (see docs/benchmark-adapters.md).

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
)

// Task is one of the seven tasks, by the id used in the results.
type Task string

const (
	TypedLoad     Task = "typed-load"
	GenericLoad   Task = "generic-load"
	Dump          Task = "dump"
	DocumentParse Task = "document-parse"
	UnchangedEmit Task = "unchanged-emit"
	EditScript    Task = "edit-script"
	PatchTask     Task = "patch"
)

var allTasks = []Task{TypedLoad, GenericLoad, Dump, DocumentParse, UnchangedEmit, EditScript, PatchTask}

func parseTask(s string) (Task, bool) {
	for _, t := range allTasks {
		if string(t) == s {
			return t, true
		}
	}
	return "", false
}

// Op is one edit or patch step, as in RFC 6902. Value is in the common form.
type Op struct {
	Op       string
	Path     string
	Value    any
	HasValue bool
}

// Native converts a common-form value to plain Go values for a library's API: integers
// that fit are int64, the rest stay text; floats are float64.
func native(v any) any {
	switch x := v.(type) {
	case Int:
		if i, err := strconv.ParseInt(string(x), 10, 64); err == nil {
			return i
		}
		return string(x)
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = native(e)
		}
		return out
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			out[k] = native(e)
		}
		return out
	}
	return v
}

// Workload is a workload as the guard and the libraries see it.
type Workload struct {
	ID        string
	Input     []byte
	Documents int
	Tasks     []Task
	Typed     string
	// Expected values by variant name: "spec" first, then e.g. "merged".
	Expected     []Named[any]
	EditOps      []Op
	EditExpected []Named[[]byte]
	PatchOps     []Op
	PatchFailing []Op
	PatchExpect  []Named[[]byte]
}

// Named is an accepted expectation and the variant it stands for.
type Named[T any] struct {
	Name string
	Val  T
}

func readJSONFile(p string) (any, error) {
	b, err := os.ReadFile(p)
	if err != nil {
		return nil, err
	}
	v, err := fromJSON(b)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", p, err)
	}
	return v, nil
}

func loadOps(dir, name, key string) ([]Op, error) {
	p := filepath.Join(dir, name)
	if _, err := os.Stat(p); err != nil {
		return nil, nil
	}
	v, err := readJSONFile(p)
	if err != nil {
		return nil, err
	}
	var out []Op
	list, _ := v.(map[string]any)[key].([]any)
	for _, e := range list {
		m, _ := e.(map[string]any)
		op := Op{}
		op.Op, _ = m["op"].(string)
		op.Path, _ = m["path"].(string)
		op.Value, op.HasValue = m["value"]
		out = append(out, op)
	}
	return out, nil
}

func loadWorkload(dir string) (*Workload, error) {
	meta, err := readJSONFile(filepath.Join(dir, "workload.json"))
	if err != nil {
		return nil, err
	}
	m, _ := meta.(map[string]any)
	in, err := os.ReadFile(filepath.Join(dir, "input.yaml"))
	if err != nil {
		return nil, err
	}
	w := &Workload{Input: in, Documents: 1}
	w.ID, _ = m["id"].(string)
	if d, ok := m["documents"].(Int); ok {
		n, _ := strconv.Atoi(string(d))
		w.Documents = n
	}
	w.Typed, _ = m["typed_model"].(string)
	tasks, _ := m["tasks"].([]any)
	for _, t := range tasks {
		if s, ok := t.(string); ok {
			if task, ok := parseTask(s); ok {
				w.Tasks = append(w.Tasks, task)
			}
		}
	}
	spec, err := readJSONFile(filepath.Join(dir, "expected.json"))
	if err != nil {
		return nil, err
	}
	w.Expected = []Named[any]{{"spec", spec}}
	if merged, err := readJSONFile(filepath.Join(dir, "expected.merged.json")); err == nil {
		w.Expected = append(w.Expected, Named[any]{"merged", merged})
	}
	if w.EditOps, err = loadOps(dir, "edit.json", "ops"); err != nil {
		return nil, err
	}
	if w.PatchOps, err = loadOps(dir, "patch.json", "ops"); err != nil {
		return nil, err
	}
	if w.PatchFailing, err = loadOps(dir, "patch.json", "failing_ops"); err != nil {
		return nil, err
	}
	// Only the plain outcome counts: an edit changes the value and nothing else.
	if b, err := os.ReadFile(filepath.Join(dir, "edit.expected.yaml")); err == nil {
		w.EditExpected = []Named[[]byte]{{"spec", b}}
	}
	if b, err := os.ReadFile(filepath.Join(dir, "patch.expected.yaml")); err == nil {
		w.PatchExpect = []Named[[]byte]{{"spec", b}}
	}
	return w, nil
}

// workloadIDs lists the workloads of the set, narrowed to `only` when it is not empty. They are
// loaded one at a time, so what the harness holds while a cell is timed is only the workload
// under test, not the whole set that the collector would otherwise scan during the clock.
func workloadIDs(dir string, only []string) ([]string, error) {
	idx, err := readJSONFile(filepath.Join(dir, "index.json"))
	if err != nil {
		return nil, err
	}
	var out []string
	list, _ := idx.(map[string]any)["workloads"].([]any)
	for _, e := range list {
		id, _ := e.(map[string]any)["id"].(string)
		if len(only) == 0 || contains(only, id) {
			out = append(out, id)
		}
	}
	return out, nil
}

func contains(l []string, s string) bool {
	for _, x := range l {
		if x == s {
			return true
		}
	}
	return false
}

// ---- products and runners ----------------------------------------------------------------

// Product is what a runner's answer becomes for the guard.
type Product struct {
	Kind ProductKind
	// Value is the answer in the common form (Value and Document kinds). HasValue is false
	// for a document the library cannot read back as data.
	Value    any
	HasValue bool
	// Bytes is the output text (Bytes kind) or what a document writes back (Document kind).
	Bytes []byte
	// ValueOnly marks a library that cannot keep the text of a document it edits: when its
	// bytes are not the expected ones, the value they hold is judged instead.
	ValueOnly bool
}

type ProductKind int

const (
	ValueProduct ProductKind = iota
	BytesProduct
	DocumentProduct
)

// FailedPatch is what a library did with the failing patch.
type FailedPatch struct {
	Errored bool
	// Bytes is what the document holds afterwards.
	Bytes []byte
}

// Batch is prepared iterations of a task. Run performs all of them and drops their results.
type Batch interface {
	Run()
	// Panic is the first panic Run recovered, or nil.
	Panic() any
}

// Runner is one library's way to do one task on one workload.
type Runner interface {
	// Check runs the task once, as it is timed, and converts the result for the guard.
	// An error is the library rejecting the input.
	Check() (Product, error)
	// CheckFailing applies the failing patch (patch runners only).
	CheckFailing() *FailedPatch
	// Prepare builds n independent starting states, outside the clock.
	Prepare(n int) Batch
}

type fnRunner[S, O any] struct {
	setup   func() S
	run     func(*S) (O, error)
	product func(O) Product
	failing func() FailedPatch
}

type fnBatch[S, O any] struct {
	states []S
	run    func(*S) (O, error)
	panicV any
}

func (b *fnBatch[S, O]) Run() {
	defer func() {
		if r := recover(); r != nil {
			b.panicV = r
		}
	}()
	for i := range b.states {
		// The result is dropped inside the clock, as a caller's would be. The starting state is
		// not: it stays in the batch and is released after the clock stops.
		out, _ := b.run(&b.states[i])
		runtime.KeepAlive(out)
	}
}

func (b *fnBatch[S, O]) Panic() any { return b.panicV }

func (r *fnRunner[S, O]) Check() (Product, error) {
	st := r.setup()
	out, err := r.run(&st)
	if err != nil {
		return Product{}, err
	}
	return r.product(out), nil
}

func (r *fnRunner[S, O]) CheckFailing() *FailedPatch {
	if r.failing == nil {
		return nil
	}
	f := r.failing()
	return &f
}

func (r *fnRunner[S, O]) Prepare(n int) Batch {
	b := &fnBatch[S, O]{states: make([]S, n), run: r.run}
	for i := range b.states {
		b.states[i] = r.setup()
	}
	return b
}

// newRunner builds a runner. setup makes one starting state (not timed), run is the timed
// task and borrows that state, and product converts the output for the guard (not timed).
func newRunner[S, O any](setup func() S, run func(*S) (O, error), product func(O) Product) Runner {
	return &fnRunner[S, O]{setup: setup, run: run, product: product}
}

// newPatchRunner is newRunner for a patch task that also checks the failing patch.
func newPatchRunner[S, O any](setup func() S, run func(*S) (O, error), product func(O) Product,
	failing func() FailedPatch) Runner {
	return &fnRunner[S, O]{setup: setup, run: run, product: product, failing: failing}
}

// Library is a library under test.
type Library interface {
	ID() string
	// Package is the module measured; the harness reads its version from the build.
	Package() string
	Role() string
	Settings() map[string]string
	// Runner returns the way to do the task, or the reason it cannot.
	Runner(task Task, w *Workload) (Runner, string)
}

// ---- pointers ------------------------------------------------------------------------------

// Seg is a step of a path into a document.
type Seg struct {
	Key   string
	Index int
	IsIdx bool
}

func pointerTokens(ptr string) []string {
	if ptr == "" {
		return nil
	}
	toks := strings.Split(ptr[1:], "/")
	for i, t := range toks {
		toks[i] = strings.ReplaceAll(strings.ReplaceAll(t, "~1", "/"), "~0", "~")
	}
	return toks
}

// pointerSegments resolves a pointer against the workload's value: a token is an index where
// the value there is a sequence and a key where it is a mapping.
func pointerSegments(base any, ptr string) ([]Seg, error) {
	cur := base
	var out []Seg
	for _, t := range pointerTokens(ptr) {
		switch c := cur.(type) {
		case []any:
			n, err := strconv.Atoi(t)
			if err != nil || n < 0 || n >= len(c) {
				return nil, fmt.Errorf("no element %q in %s", t, ptr)
			}
			out = append(out, Seg{Index: n, IsIdx: true})
			cur = c[n]
		case map[string]any:
			v, ok := c[t]
			if !ok {
				return nil, fmt.Errorf("no key %q in %s", t, ptr)
			}
			out = append(out, Seg{Key: t})
			cur = v
		default:
			return nil, fmt.Errorf("cannot descend into a scalar at %q in %s", t, ptr)
		}
	}
	return out, nil
}

// EditStep is an edit with its path resolved before the clock.
type EditStep struct {
	Op     string
	Segs   []Seg
	Value  any // native Go value
	Dotted string
}

func resolveSteps(w *Workload, ops []Op) ([]EditStep, error) {
	base := w.Expected[0].Val
	var out []EditStep
	for _, o := range ops {
		segs, err := pointerSegments(base, o.Path)
		if err != nil {
			return nil, err
		}
		out = append(out, EditStep{Op: o.Op, Segs: segs, Value: native(o.Value), Dotted: o.Path})
	}
	return out, nil
}
