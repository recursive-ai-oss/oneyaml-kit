package main

import (
	"strings"
	"sync"
	"testing"
	"time"
)

var keep [][]byte

func TestExplainBytesNamesTheKindOfDifference(t *testing.T) {
	for _, c := range []struct{ got, want, says string }{
		{"a: 1\nb: 2\n", "a: 1\n\nb: 2\n", "only in blank lines"},
		{"a:\n  b: 1\n", "a:\n    b: 1\n", "only in indentation"},
		{"a: 'x'\n", "a: x\n", "only in quoting"},
		{"a: {x: 1,   y: 2}\n", "a: {x: 1, y: 2}\n", "only in spacing inside lines"},
		{"a: 1\r\nb: 2\r\n", "a: 1\nb: 2\n", "only in line breaks"},
		{"a: 1\nb: 2\n", "a: 1\n\n  b: 2\n", "only in blank lines and indentation"},
		{"a: 1\nb: 2\n", "a: 1\nb: 3\n", `line 2: got "b: 2", expected "b: 3"`},
		{"a: 1\n", "a: 1\nb: 3\n", `expected "b: 3"`},
	} {
		d := bytesDiff([]byte(c.got), []byte(c.want))
		if !strings.Contains(d, c.says) {
			t.Errorf("%q vs %q: %s", c.got, c.want, d)
		}
	}
	if bytesDiff([]byte("same"), []byte("same")) != "" {
		t.Error("equal texts differ")
	}
}

// The memory pass reports what a run leaves behind, so a library that keeps buffers is seen.
func TestRetainedBytesSeeWhatARunKeeps(t *testing.T) {
	keep = nil
	leaky := newRunner(func() struct{} { return struct{}{} },
		func(*struct{}) ([]byte, error) { keep = append(keep, make([]byte, 1<<20)); return nil, nil },
		bytesProduct)
	if m := memoryPass(leaky, 3, 3); m.RetainedBytes < 1<<19 {
		t.Errorf("a run that keeps a megabyte retained %d bytes", m.RetainedBytes)
	}
	keep = nil
	clean := newRunner(func() struct{} { return struct{}{} },
		func(*struct{}) ([]byte, error) { return make([]byte, 1<<20), nil },
		bytesProduct)
	if m := memoryPass(clean, 3, 3); m.RetainedBytes > 1<<18 {
		t.Errorf("a run that keeps nothing retained %d bytes", m.RetainedBytes)
	}
}

// A state the task only reads is live before and after the run, so it is not what the run kept.
func TestRetainedBytesDoNotCountTheHeldStartingState(t *testing.T) {
	reads := newRunner(func() []byte { return make([]byte, 8<<20) },
		func(s *[]byte) ([]byte, error) { return nil, nil },
		bytesProduct)
	m := memoryPass(reads, 3, 3)
	if m.RetainedBytes > 1<<20 {
		t.Errorf("a held state retained %d bytes", m.RetainedBytes)
	}
	if m.PeakBytes < 8<<20 {
		t.Errorf("the state counts in the peak: %d", m.PeakBytes)
	}
}

// A full run keeps at least thirty samples in every cell; fewer are for quick mode only.
func TestFullModeKeepsAtLeastThirtySamples(t *testing.T) {
	if fullConfig().Samples < 30 {
		t.Errorf("full mode takes %d samples", fullConfig().Samples)
	}
	if quickConfig().Samples >= 30 {
		t.Error("quick mode is the smoke tier")
	}
}

var (
	sink     []byte
	coldPool bool
)

// coldStartRunner allocates 40 times per run, and 6 more in the first run after its starting
// states were built, as a library does whose pools a collection has just emptied.
func coldStartRunner() Runner {
	return newRunner(func() struct{} { coldPool = true; return struct{}{} },
		func(*struct{}) ([]byte, error) {
			for i := 0; i < 40; i++ {
				sink = make([]byte, 100)
			}
			if coldPool {
				coldPool = false
				for i := 0; i < 6; i++ {
					sink = make([]byte, 100)
				}
			}
			return nil, nil
		}, bytesProduct)
}

// A library that keeps a buffer in a sync.Pool allocates a few dozen times in the first run after a
// collection and twice in every run after it. A user pays the first once per collection; a timed
// sample of three runs that followed a collection would pay it on every sample, and report five
// times the steady state. The clock must start with the pool full, in the timed samples and in the
// memory pass alike, so both read the steady state and agree.
var (
	poolSink []byte
	poolNews int
	bufPool  = sync.Pool{New: func() any {
		poolNews++
		parts := make([][]byte, 30)
		for i := range parts {
			parts[i] = make([]byte, 16)
		}
		return &parts
	}}
)

// pooledRunner takes about 300 microseconds a run, so a one-millisecond sample holds three or four.
func pooledRunner() Runner {
	return newRunner(func() struct{} { return struct{}{} },
		func(*struct{}) ([]byte, error) {
			buf := bufPool.Get().(*[][]byte)
			for t0 := time.Now(); time.Since(t0) < 300*time.Microsecond; {
			}
			out := make([]byte, 64) // the answer
			poolSink = make([]byte, 8)
			bufPool.Put(buf)
			return out, nil
		}, bytesProduct)
}

func TestPoolBackedLibraryReadsTheSteadyStateInBothPasses(t *testing.T) {
	r := pooledRunner()
	tm := timeRunner(r, quickConfig())
	if tm.ItersPerSample > 10 {
		t.Fatalf("the sample holds %d runs; the test needs a few", tm.ItersPerSample)
	}
	m := memoryPass(r, avgRunsFor(tm, quickConfig()), min(tm.ItersPerSample, tm.Chunk))
	if tm.AllocsPerOp < 1.9 || tm.AllocsPerOp > 2.5 {
		t.Errorf("testing.B counted %.1f allocations a run; the steady state is 2", tm.AllocsPerOp)
	}
	if m.Allocations != 2 {
		t.Errorf("the memory pass counted %d allocations a run; the steady state is 2", m.Allocations)
	}
	if d := agree(tm, m); d != "" {
		t.Error(d)
	}
}

// With the pool full the timed runs of a task with a cold start read the same as the steady state.
func TestWarmUpRemovesTheColdStartFromTheTimedRuns(t *testing.T) {
	steady := Timing{ItersPerSample: 2, Chunk: 2, AllocsPerOp: 40, BytesPerOp: 40 * 112}
	m := memoryPass(coldStartRunner(), 64, 2)
	if d := agree(steady, m); d != "" {
		t.Errorf("the memory pass must read the steady state: %v (%+v)", d, m)
	}
	if m.Allocations != 40 {
		t.Errorf("expected 40 allocations a run, got %d", m.Allocations)
	}
}

// Warming up must not hide a real difference: a figure twice the steady state is still refused.
func TestWarmedMemoryPassStillRefusesARealDisagreement(t *testing.T) {
	timed := Timing{ItersPerSample: 2, Chunk: 2, AllocsPerOp: 80, BytesPerOp: 80 * 112}
	m := memoryPass(coldStartRunner(), 64, 2)
	if agree(timed, m) == "" {
		t.Errorf("a doubled allocation count was accepted: %+v", m)
	}
}

var (
	lostRuns  int
	lostSink  [][]byte
	lostEvery = 61
)

// lossyPoolRunner stands for a library whose pooled state is dropped now and then, when a natural
// collection lands between two calls: every 61st run (counted over the whole process) allocates the
// state again, 2000 calls and about 200 kB. It takes about 300 microseconds a run, so a sample holds
// a few runs and a miss lands in one sample or one group, never in most.
func lossyPoolRunner() Runner {
	return newRunner(func() struct{} { return struct{}{} },
		func(*struct{}) ([]byte, error) {
			lostRuns++
			for t0 := time.Now(); time.Since(t0) < 300*time.Microsecond; {
			}
			poolSink = make([]byte, 8)
			if lostRuns%lostEvery == 0 {
				lostSink = lostSink[:0]
				for i := 0; i < 2000; i++ {
					lostSink = append(lostSink, make([]byte, 100))
				}
			}
			return make([]byte, 64), nil
		}, bytesProduct)
}

// A refill that one run in 61 pays is not what a run costs: the figure of both passes is the
// middle of the groups, which is 2 calls a run. A mean of the same runs reads 2 + 2000/61 = 35.
func TestARareRefillDoesNotMoveEitherPass(t *testing.T) {
	r := lossyPoolRunner()
	tm := timeRunner(r, quickConfig())
	m := memoryPass(r, avgRunsFor(tm, quickConfig()), min(tm.ItersPerSample, tm.Chunk))
	if tm.AllocsPerOp != 2 {
		t.Errorf("the timed figure is %.1f calls a run, the steady state is 2", tm.AllocsPerOp)
	}
	if m.Allocations != 2 {
		t.Errorf("the memory pass counted %d calls a run, the steady state is 2", m.Allocations)
	}
	if d := agree(tm, m); d != "" {
		t.Error(d)
	}
}

// With the refill in every group the median is the refill, in both passes: it is a cost of the
// library, and the check still catches a real difference between the passes.
func TestAMedianStillRefusesARealDisagreement(t *testing.T) {
	tm := Timing{ItersPerSample: 3, Chunk: 3, AllocsPerOp: 4, BytesPerOp: 4 * 112}
	lostRuns = 0
	m := memoryPass(lossyPoolRunner(), 63, 3)
	if m.Allocations != 2 {
		t.Fatalf("expected the steady 2 calls, got %d", m.Allocations)
	}
	if agree(tm, m) == "" {
		t.Error("a timed figure of twice the memory pass was accepted")
	}
}
