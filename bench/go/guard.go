package main

// The correctness guard: judges what a library produced before anything is timed. The
// comparison is complete: the whole value, every output byte. A library that fails the guard
// gets a verdict and a reason in the results and no numbers.

import (
	"fmt"
	"strings"
)

// Verdict is the outcome of the guard for one cell.
type Verdict struct {
	Status  string // ok, differs, rejects, unjudged
	Detail  string
	Variant string
}

func ok(variant string) Verdict { return Verdict{Status: "ok", Variant: variant} }

// refereeError says why the reference parsers gave no single reading of some text.
type refereeError struct {
	rejected bool // no reference parser accepts it: the text is not YAML
	msg      string
}

func (e *refereeError) Error() string { return e.msg }

type referee func([]byte) (any, *refereeError)

func matchValue(w *Workload, got any) Verdict {
	var diffs []string
	for _, e := range w.Expected {
		d := firstDiff(got, e.Val, "")
		if d == "" {
			return ok(e.Name)
		}
		if e.Name == "spec" {
			diffs = append(diffs, d)
		} else {
			diffs = append(diffs, e.Name+": "+d)
		}
	}
	return Verdict{Status: "differs", Detail: strings.Join(diffs, "; ")}
}

func matchBytes(got []byte, want []Named[[]byte], what string) Verdict {
	first := ""
	for _, e := range want {
		d := bytesDiff(got, e.Val)
		if d == "" {
			return ok(e.Name)
		}
		if first == "" {
			first = d
		}
	}
	if first == "" {
		first = "workload has no expected " + what
	}
	return Verdict{Status: "differs", Detail: first}
}

// judge decides one library's answer to one task.
func judge(task Task, w *Workload, p Product, err error, failing *FailedPatch, ref referee) Verdict {
	if err != nil {
		return Verdict{Status: "rejects", Detail: clip(err.Error())}
	}
	var v Verdict
	switch task {
	case TypedLoad, GenericLoad:
		if p.Kind != ValueProduct {
			return Verdict{Status: "differs", Detail: "the runner gave no value"}
		}
		v = matchValue(w, p.Value)
	case Dump:
		got, rerr := ref(p.Bytes)
		switch {
		case rerr != nil && rerr.rejected:
			v = Verdict{Status: "differs", Detail: "the output does not parse: " + clip(rerr.msg)}
		case rerr != nil:
			v = Verdict{Status: "unjudged", Detail: clip(rerr.msg)}
		default:
			// Read back, the output must hold the value, or the merged value when the library
			// applies merge keys while loading.
			v = matchValue(w, got)
			if v.Status == "differs" {
				v.Detail = "read back: " + v.Detail
			}
		}
	case DocumentParse:
		// A lossless document must hold the value and write the input back unchanged.
		by := ok("bytes-only")
		if p.HasValue {
			by = matchValue(w, p.Value)
		}
		d := bytesDiff(p.Bytes, w.Input)
		switch {
		case by.Status == "ok" && d == "":
			v = by
		case by.Status == "ok":
			v = Verdict{Status: "differs", Detail: "does not write the input back: " + d}
		default:
			v = by
		}
	case UnchangedEmit:
		if d := bytesDiff(p.Bytes, w.Input); d != "" {
			v = Verdict{Status: "differs", Detail: d}
		} else {
			v = ok("spec")
		}
	case EditScript:
		v = matchBytes(p.Bytes, w.EditExpected, "edit output")
	case PatchTask:
		v = matchBytes(p.Bytes, w.PatchExpect, "patch output")
		if v.Status == "differs" && p.ValueOnly {
			v = valueOnly(p, w, ref, v)
		}
		if v.Status == "ok" {
			switch {
			case failing == nil:
				v = Verdict{Status: "differs", Detail: "the runner did not apply the failing patch"}
			case !failing.Errored:
				v = Verdict{Status: "differs", Detail: "a patch with a failing step reported success"}
			default:
				if d := bytesDiff(failing.Bytes, w.Input); d != "" {
					v = Verdict{Status: "differs", Detail: "a failed patch changed the document: " + d}
				}
			}
		}
	default:
		v = Verdict{Status: "differs", Detail: fmt.Sprintf("task %s has no judge", task)}
	}
	return v
}

// valueOnly judges the output of a library that cannot keep the text: both the output and the
// expected bytes are read by the reference parsers, and the values must be equal. The
// result is the variant `value-only`, which is a known deviation, never a plain pass.
func valueOnly(p Product, w *Workload, ref referee, exact Verdict) Verdict {
	got, e1 := ref(p.Bytes)
	if e1 != nil {
		if e1.rejected {
			return Verdict{Status: "differs", Detail: "the output does not parse: " + clip(e1.msg)}
		}
		return Verdict{Status: "unjudged", Detail: clip(e1.msg)}
	}
	for _, e := range w.PatchExpect {
		want, e2 := ref(e.Val)
		if e2 != nil {
			return Verdict{Status: "unjudged", Detail: "the expected bytes cannot be read: " + clip(e2.msg)}
		}
		if d := firstDiff(got, want, ""); d != "" {
			return Verdict{Status: "differs", Detail: "value after the patch: " + d}
		}
		return ok("value-only")
	}
	return exact
}
