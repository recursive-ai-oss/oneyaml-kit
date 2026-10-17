package main

// Every prepared state owns its input. A library that keeps the buffer it is given, slices it or
// reads it lazily would otherwise see one cache-hot buffer in all the states of a batch while a
// library that copies sees many cold ones, and the comparison would depend on the harness. These
// tests walk every state's memory and refuse any two states, or a state and the workload, that
// reach overlapping input-sized buffers.

import (
	"fmt"
	"reflect"
	"slices"
	"strings"
	"testing"
	"unsafe"
)

type span struct{ lo, hi uintptr }

type visit struct {
	p uintptr
	t reflect.Type
	n int
}

func seenBefore(seen map[visit]bool, k visit) bool {
	if seen[k] {
		return true
	}
	seen[k] = true
	return false
}

// bigBuffers collects the address ranges of every string and byte slice of at least minLen bytes that
// can be reached from v.
func bigBuffers(v reflect.Value, minLen int, seen map[visit]bool, out *[]span) {
	switch v.Kind() {
	case reflect.String:
		if v.Len() >= minLen {
			lo := uintptr(unsafe.Pointer(unsafe.StringData(v.String())))
			*out = append(*out, span{lo, lo + uintptr(v.Len())})
		}
	case reflect.Slice:
		if v.Len() == 0 {
			return
		}
		if v.Type().Elem().Kind() == reflect.Uint8 {
			if v.Len() >= minLen {
				lo := v.Pointer()
				*out = append(*out, span{lo, lo + uintptr(v.Len())})
			}
			return
		}
		if seenBefore(seen, visit{v.Pointer(), v.Type(), v.Len()}) {
			return
		}
		for i := 0; i < v.Len(); i++ {
			bigBuffers(v.Index(i), minLen, seen, out)
		}
	case reflect.Array:
		if v.Type().Elem().Kind() == reflect.Uint8 {
			return
		}
		for i := 0; i < v.Len(); i++ {
			bigBuffers(v.Index(i), minLen, seen, out)
		}
	case reflect.Pointer:
		if v.IsNil() {
			return
		}
		if seenBefore(seen, visit{v.Pointer(), v.Type(), 0}) {
			return
		}
		bigBuffers(v.Elem(), minLen, seen, out)
	case reflect.Interface:
		if !v.IsNil() {
			bigBuffers(v.Elem(), minLen, seen, out)
		}
	case reflect.Map:
		if v.IsNil() {
			return
		}
		if seenBefore(seen, visit{v.Pointer(), v.Type(), 0}) {
			return
		}
		for it := v.MapRange(); it.Next(); {
			bigBuffers(it.Key(), minLen, seen, out)
			bigBuffers(it.Value(), minLen, seen, out)
		}
	case reflect.Struct:
		for i := 0; i < v.NumField(); i++ {
			bigBuffers(v.Field(i), minLen, seen, out)
		}
	}
}

// sharedInput names the first problem with the input a batch of states holds: two states, or a
// state and the workload, reaching overlapping input-sized buffers; two states reaching the same
// object (a parsed document, a tree, a map) by identity; or, when mustHold is set, a state that
// holds no input-sized buffer at all although its task reads the input inside the clock.
func sharedInput(states []any, master []byte, minLen int, mustHold bool) string {
	var held [][]span
	var objects []map[visit]bool
	for _, s := range states {
		var spans []span
		seen := map[visit]bool{}
		bigBuffers(reflect.ValueOf(s), minLen, seen, &spans)
		held = append(held, spans)
		objects = append(objects, seen)
	}
	overlap := func(a, b span) bool { return a.lo < b.hi && b.lo < a.hi }
	var m span
	if len(master) > 0 {
		lo := uintptr(unsafe.Pointer(&master[0]))
		m = span{lo, lo + uintptr(len(master))}
	}
	for i, a := range held {
		if mustHold && len(a) == 0 {
			return fmt.Sprintf("state %d holds no copy of the input its task reads", i)
		}
		for _, x := range a {
			if m.hi != 0 && overlap(x, m) {
				return fmt.Sprintf("state %d reads the workload's own input", i)
			}
			for j := i + 1; j < len(held); j++ {
				for _, y := range held[j] {
					if overlap(x, y) {
						return fmt.Sprintf("states %d and %d share an input buffer", i, j)
					}
				}
			}
		}
	}
	for i := range objects {
		for j := i + 1; j < len(objects); j++ {
			for k := range objects[i] {
				if objects[j][k] && !sharedByDesign(k) {
					return fmt.Sprintf("states %d and %d reach the same %v", i, j, k.t)
				}
			}
		}
	}
	return ""
}

func ownershipWorkload(t *testing.T) *Workload {
	var b strings.Builder
	b.WriteString("# top\nname: demo\nitems:\n")
	for i := 0; i < 60; i++ {
		fmt.Fprintf(&b, "  - id: %d\n    label: \"entry number %d\"\n", i, i)
	}
	return &Workload{ID: "owned", Input: []byte(b.String()), Documents: 1, Tasks: allTasks, Typed: "records",
		Expected:     []Named[any]{{"spec", mustJSON(t, `{"name": "demo", "items": []}`)}},
		EditOps:      []Op{{Op: "replace", Path: "/name", Value: "other", HasValue: true}},
		PatchOps:     []Op{{Op: "replace", Path: "/name", Value: "other", HasValue: true}},
		PatchFailing: []Op{{Op: "remove", Path: "/nope"}}}
}

// sharedByDesign is for objects every state may reach: zero-size values all live at one address.
func sharedByDesign(k visit) bool {
	return k.t.Kind() == reflect.Pointer && k.t.Elem().Size() == 0
}

func TestNoTwoPreparedStatesShareAnInput(t *testing.T) {
	w := ownershipWorkload(t)
	checked := map[string]bool{}
	for _, l := range allLibraries() {
		if l.Role() == "control" {
			continue
		}
		for _, task := range allTasks {
			r, _ := l.Runner(task, w)
			if r == nil {
				continue
			}
			states := r.Prepare(4).States()
			if len(states) != 4 {
				t.Fatalf("%s %s: %d states", l.ID(), task, len(states))
			}
			// These tasks read the input inside the clock, so each state must hold a copy of it.
			reads := task == TypedLoad || task == GenericLoad || task == DocumentParse || task == PatchTask
			if why := sharedInput(states, w.Input, len(w.Input)/2, reads); why != "" {
				t.Errorf("%s %s: %s", l.ID(), task, why)
			}
			checked[l.ID()+" "+string(task)] = true
		}
	}
	if len(checked) < 10 {
		t.Errorf("only %d library and task pairs were checked", len(checked))
	}
	for _, pair := range []string{"yaml-v3 unchanged-emit", "goccy-go-yaml document-parse", "yaml-v4 generic-load"} {
		if !checked[pair] {
			t.Errorf("%s was not checked", pair)
		}
	}
}

type holder struct {
	name string
	data any
}

func TestTheSharingCheckFindsSharedBuffers(t *testing.T) {
	master := make([]byte, 100)
	buf := make([]byte, 100)
	text := strings.Repeat("x", 100)
	doc := &holder{"tree", map[string]any{"k": 1}}
	other := []byte(strings.Repeat("y", 100))

	own := func(b []byte) any { return &holder{"a", [][]byte{b}} }
	if why := sharedInput([]any{own(buf), own(slices.Clone(buf))}, master, 50, true); why != "" {
		t.Errorf("distinct buffers were refused: %s", why)
	}
	for name, states := range map[string][]any{
		"one slice in two states":   {own(buf), own(buf)},
		"a sub-slice of the other":  {own(buf[:60]), own(buf[30:])},
		"a string in two states":    {&holder{"a", text}, &holder{"b", map[string]any{"k": []any{text}}}},
		"a hidden field":            {&struct{ b []byte }{buf}, &struct{ b []byte }{buf}},
		"the workload's own bytes":  {own(other), own(master)},
		"a shared parsed object":    {&holder{"a", doc}, &holder{"b", doc}},
		"a buffer behind a pointer": {&holder{"a", &buf}, &holder{"b", &buf}},
	} {
		if sharedInput(states, master, 50, false) == "" {
			t.Errorf("%s was not found", name)
		}
	}
}

func TestAStateThatReadsItsInputMustHoldACopy(t *testing.T) {
	master := make([]byte, 100)
	if sharedInput([]any{&struct{}{}, &struct{}{}}, master, 50, true) == "" {
		t.Error("states that hold nothing were accepted for a task that reads the input")
	}
	if why := sharedInput([]any{&struct{}{}, &struct{}{}}, master, 50, false); why != "" {
		t.Errorf("states that need no copy were refused: %s", why)
	}
}
