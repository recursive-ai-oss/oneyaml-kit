package main

import (
	"strings"
	"testing"
)

// A task that only reads its starting state may run a batch over one state, and then builds one.
func TestAReadOnlyTaskSharesOneState(t *testing.T) {
	setups := 0
	r := newRunner(func() []byte { setups++; return []byte("a: 1\n") },
		func(s *[]byte) ([]byte, error) { return *s, nil }, bytesProduct)
	if d := shareState(UnchangedEmit, r); d != "" {
		t.Fatal(d)
	}
	setups = 0
	b := r.Prepare(10000)
	b.Run()
	if setups != 1 {
		t.Errorf("a shared batch built %d starting states", setups)
	}
	if b.Panic() != nil {
		t.Error(b.Panic())
	}
}

// The task changes its state: the second answer differs, so the runner is refused.
func TestARunnerThatChangesItsStateIsRefused(t *testing.T) {
	r := newRunner(func() []byte { return []byte("a") },
		func(s *[]byte) ([]byte, error) { *s = append(*s, 'a'); return append([]byte(nil), *s...), nil },
		bytesProduct)
	d := shareState(Dump, r)
	if !strings.Contains(d, "changes its starting state") {
		t.Errorf("a task that appends to its state was accepted: %q", d)
	}
	// A value answer that changes is refused the same way.
	n := 0
	v := newRunner(func() struct{} { return struct{}{} },
		func(*struct{}) (int, error) { n++; return n, nil },
		func(i int) Product {
			return Product{Kind: ValueProduct, Value: Int(string(rune('0' + i))), HasValue: true}
		})
	if d := shareState(GenericLoad, v); !strings.Contains(d, "different value") {
		t.Errorf("a changing value was accepted: %q", d)
	}
}

// An edit or a patch applies in place, so it always gets a state of its own, whatever it answers.
func TestEditsAndPatchesNeverShare(t *testing.T) {
	setups := 0
	r := newRunner(func() int { setups++; return 0 },
		func(*int) ([]byte, error) { return []byte("x"), nil }, bytesProduct)
	for _, task := range []Task{EditScript, PatchTask} {
		if d := shareState(task, r); d != "" {
			t.Fatal(d)
		}
	}
	setups = 0
	r.Prepare(5)
	if setups != 5 {
		t.Errorf("an edit batch built %d states instead of 5", setups)
	}
}

// A shared batch is not limited by the size of a state: its chunk is as long as the plan allows.
func TestASharedBatchIsNotChunkedByStateSize(t *testing.T) {
	r := newRunner(func() []byte { return make([]byte, 8<<20) },
		func(s *[]byte) ([]byte, error) { return nil, nil }, bytesProduct)
	cfg := quickConfig()
	if c := chunkSize(r, cfg); c >= cfg.MaxIters {
		t.Fatalf("the unshared chunk should be limited by the 8 MB state: %d", c)
	}
	shareState(UnchangedEmit, r)
	if c := chunkSize(r, cfg); c != cfg.MaxIters {
		t.Errorf("a shared batch got a chunk of %d", c)
	}
}
