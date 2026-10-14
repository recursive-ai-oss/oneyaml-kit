//go:build oneyaml_go_src

package main

import (
	"errors"
	"runtime"

	"oneyaml.dev/go"
)

// sharedRunner is a runner for a task that only reads its starting state. A batch runs the
// task n times over one state, as the harness documentation allows: building n copies of a
// document of a few hundred kilobytes would cost far more than the task being timed.
type sharedRunner struct {
	setup func() *oneyaml.Document
	run   func(*oneyaml.Document) ([]byte, error)
}

func (r sharedRunner) Check() (Product, error) {
	out, err := r.run(r.setup())
	if err != nil {
		return Product{}, err
	}
	return bytesProduct(out), nil
}

func (sharedRunner) CheckFailing() *FailedPatch { return nil }

func (r sharedRunner) Prepare(n int) Batch {
	return &sharedBatch{doc: r.setup(), run: r.run, n: n}
}

type sharedBatch struct {
	doc    *oneyaml.Document
	run    func(*oneyaml.Document) ([]byte, error)
	n      int
	panicV any
}

func (b *sharedBatch) Run() {
	defer func() {
		if r := recover(); r != nil {
			b.panicV = r
		}
	}()
	for i := 0; i < b.n; i++ {
		out, _ := b.run(b.doc)
		runtime.KeepAlive(out)
	}
}

func (b *sharedBatch) Panic() any { return b.panicV }

// Runner shares one parsed document across the batch for unchanged-emit, which only reads
// it, and defers to the common runners for every other task.
func (l ownLib) Runner(task Task, w *Workload) (Runner, string) {
	if task != UnchangedEmit {
		return l.yamlLib.Runner(task, w)
	}
	in := w.Input
	return sharedRunner{
		setup: func() *oneyaml.Document {
			d, err := oneyaml.Parse(in)
			if err != nil {
				return nil
			}
			return d
		},
		run: func(d *oneyaml.Document) ([]byte, error) {
			if d == nil {
				return nil, errors.New("the input does not parse")
			}
			return d.Bytes(), nil
		},
	}, ""
}
