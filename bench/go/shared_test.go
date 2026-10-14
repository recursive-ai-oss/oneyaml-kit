package main

import (
	"strconv"
	"strings"
	"testing"
	"time"
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

// notShared checks that a runner is not shared, and that its batches then build a state for every run.
func notShared(t *testing.T, name string, task Task, r Runner, setups *int) string {
	t.Helper()
	why := shareState(task, r)
	if why == "" {
		t.Errorf("%s: the runner was shared", name)
	}
	*setups = 0
	r.Prepare(6)
	if *setups != 6 {
		t.Errorf("%s: a batch of 6 built %d states", name, *setups)
	}
	return why
}

// The task changes its state: the second answer differs, so the runner is not shared. The library
// is not wrong, so this is not a failed cell.
func TestARunnerThatChangesItsStateIsNotShared(t *testing.T) {
	setups := 0
	r := newRunner(func() []byte { setups++; return []byte("a") },
		func(s *[]byte) ([]byte, error) { *s = append(*s, 'a'); return append([]byte(nil), *s...), nil },
		bytesProduct)
	if why := notShared(t, "append", Dump, r, &setups); !strings.Contains(why, "different bytes") {
		t.Errorf("reason: %q", why)
	}
	// A value answer that changes is the same.
	n := 0
	v := newRunner(func() struct{} { setups++; return struct{}{} },
		func(*struct{}) (int, error) { n++; return n, nil },
		func(i int) Product {
			return Product{Kind: ValueProduct, Value: Int(string(rune('0' + i))), HasValue: true}
		})
	if why := notShared(t, "value", GenericLoad, v, &setups); !strings.Contains(why, "different value") {
		t.Errorf("reason: %q", why)
	}
}

var costSink [][]byte

// The answer is the same bytes, but the work is done once per state: the second run is free.
func TestARunnerThatMemoisesInItsStateIsNotShared(t *testing.T) {
	setups := 0
	type doc struct{ out []byte }
	r := newRunner(func() doc { setups++; return doc{} },
		func(d *doc) ([]byte, error) {
			if d.out != nil {
				return d.out, nil
			}
			for i := 0; i < 50; i++ {
				costSink = append(costSink[:0], make([]byte, 64))
			}
			for t0 := time.Now(); time.Since(t0) < 200*time.Microsecond; {
			}
			d.out = []byte("a: 1\n")
			return d.out, nil
		}, bytesProduct)
	if why := notShared(t, "memo", UnchangedEmit, r, &setups); !strings.Contains(why, "allocations") {
		t.Errorf("reason: %q", why)
	}
}

// Without allocating, the second run still takes a fraction of the time.
func TestARunnerThatIsMuchFasterTheSecondTimeIsNotShared(t *testing.T) {
	setups := 0
	type doc struct{ done bool }
	r := newRunner(func() doc { setups++; return doc{} },
		func(d *doc) ([]byte, error) {
			if !d.done {
				for t0 := time.Now(); time.Since(t0) < 300*time.Microsecond; {
				}
				d.done = true
			}
			return []byte("a: 1\n"), nil
		}, bytesProduct)
	if why := notShared(t, "slow-first", UnchangedEmit, r, &setups); !strings.Contains(why, "takes") {
		t.Errorf("reason: %q", why)
	}
}

// The answer is a buffer the state owns and rewrites: compared with itself it would always match.
func TestAnAnswerThatIsTheStatesOwnRewrittenBufferIsNotShared(t *testing.T) {
	setups := 0
	type doc struct {
		buf []byte
		n   int
	}
	r := newRunner(func() doc { setups++; return doc{buf: make([]byte, 0, 32)} },
		func(d *doc) ([]byte, error) {
			d.n++
			d.buf = strconv.AppendInt(d.buf[:0], int64(d.n), 10)
			return d.buf, nil
		}, bytesProduct)
	if why := notShared(t, "alias", UnchangedEmit, r, &setups); !strings.Contains(why, "different bytes") {
		t.Errorf("reason: %q", why)
	}
}

// A task that clears a field of its input (as a dump that strips tags in place does) returns the
// same bytes and does less work the second time.
func TestARunnerThatStripsItsInputIsNotShared(t *testing.T) {
	setups := 0
	type node struct {
		tags []*string
	}
	r := newRunner(func() node {
		setups++
		n := node{}
		for i := 0; i < 40; i++ {
			s := "!!str"
			n.tags = append(n.tags, &s)
		}
		return n
	},
		func(n *node) ([]byte, error) {
			var out []byte
			for i, tag := range n.tags {
				if tag != nil {
					out = append(out, []byte(*tag)...) // work done only while the tag is there
					n.tags[i] = nil
				}
			}
			return []byte("a: 1\n"), nil
		}, bytesProduct)
	if why := notShared(t, "strip", Dump, r, &setups); !strings.Contains(why, "allocations") {
		t.Errorf("reason: %q", why)
	}
}

// An edit or a patch applies in place, so it always gets a state of its own, whatever it answers.
func TestEditsAndPatchesNeverShare(t *testing.T) {
	setups := 0
	r := newRunner(func() int { setups++; return 0 },
		func(*int) ([]byte, error) { return []byte("x"), nil }, bytesProduct)
	for _, task := range []Task{EditScript, PatchTask} {
		if d := shareState(task, r); d == "" {
			t.Fatalf("%s was shared", task)
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
