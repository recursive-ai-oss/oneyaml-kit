//go:build selftest

package main

import (
	"errors"
	"os"
)

// The control libraries are deliberately fake. One returns exactly the expectation, one an
// altered expectation, one refuses, one panics. The harness's tests run them through every
// task to prove the guard accepts a right answer, refuses a wrong one and gives a refused
// cell no numbers.

type behaviour int

const (
	behaveCorrect behaviour = iota
	behaveWrong
	behaveReject
	behavePanic
	// behaveCrash ends the process, as a library that aborts would.
	behaveCrash
)

type control struct {
	id string
	b  behaviour
}

func (c control) ID() string                  { return c.id }
func (c control) Package() string             { return "control" }
func (c control) Role() string                { return "control" }
func (c control) Settings() map[string]string { return nil }

func alter(v any) any {
	switch x := v.(type) {
	case map[string]any:
		out := map[string]any{}
		for k, e := range x {
			out[k] = e
		}
		out["altered"] = true
		return out
	case []any:
		return append(append([]any{}, x...), "altered")
	}
	return "altered"
}

func (c control) Runner(task Task, w *Workload) (Runner, string) {
	product := func() Product {
		switch task {
		case TypedLoad, GenericLoad:
			v := w.Expected[0].Val
			if c.b == behaveWrong {
				v = alter(v)
			}
			return Product{Kind: ValueProduct, Value: v, HasValue: true}
		case Dump, UnchangedEmit:
			b := append([]byte(nil), w.Input...)
			if c.b == behaveWrong {
				if task == Dump {
					b = append(b, []byte("altered: true\n")...)
				} else {
					b = append(b, ' ')
				}
			}
			return Product{Kind: BytesProduct, Bytes: b}
		case DocumentParse:
			p := Product{Kind: DocumentProduct, Value: w.Expected[0].Val, HasValue: true, Bytes: append([]byte(nil), w.Input...)}
			if c.b == behaveWrong {
				p.Bytes = append(p.Bytes, ' ')
			}
			return p
		case EditScript:
			b := append([]byte(nil), w.EditExpected[0].Val...)
			if c.b == behaveWrong {
				b = append(b, ' ')
			}
			return Product{Kind: BytesProduct, Bytes: b}
		}
		b := append([]byte(nil), w.PatchExpect[0].Val...)
		if c.b == behaveWrong {
			b = append(b, ' ')
		}
		return Product{Kind: BytesProduct, Bytes: b}
	}
	p := product()
	run := func(*struct{}) (Product, error) {
		switch c.b {
		case behaveReject:
			return Product{}, errors.New("control refuses")
		case behavePanic:
			panic("control panics")
		case behaveCrash:
			os.Exit(3)
		}
		return p, nil
	}
	setup := func() struct{} { return struct{}{} }
	if task == PatchTask {
		return newPatchRunner(setup, run, func(p Product) Product { return p }, func() FailedPatch {
			if c.b == behaveWrong {
				// A patch with a failing step that reports success and changes the document.
				return FailedPatch{Errored: false, Bytes: append([]byte(nil), w.Input...)}
			}
			return FailedPatch{Errored: true, Bytes: append([]byte(nil), w.Input...)}
		}), ""
	}
	return newRunner(setup, run, func(p Product) Product { return p }), ""
}
