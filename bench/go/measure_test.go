package main

import (
	"runtime"
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
	periodRuns int
	periodSink [][]byte
	refillGC   uint32
	refillSink [][]byte
)

// flushRunner pays a cost by design: every 7th run (counted over the whole process) it builds a
// buffer of 700 small objects, as a periodic flush or a growing buffer does. A user's program pays
// it on average, 100 calls a run on top of the 2 of an ordinary run.
func flushRunner() Runner {
	return newRunner(func() struct{} { return struct{}{} },
		func(*struct{}) ([]byte, error) {
			periodRuns++
			for t0 := time.Now(); time.Since(t0) < 100*time.Microsecond; {
			}
			poolSink = make([]byte, 8)
			if periodRuns%7 == 0 {
				periodSink = periodSink[:0]
				for i := 0; i < 700; i++ {
					periodSink = append(periodSink, make([]byte, 100))
				}
			}
			return make([]byte, 64), nil
		}, bytesProduct)
}

// A cost that is paid by design now and then is part of what a run costs, so the published figure
// has it: the mean over all the timed runs, as testing.B counts. A median of the samples would
// publish 2 here and put the library ahead of one that pays the same total on every call.
func TestAPeriodicCostIsInThePublishedFigure(t *testing.T) {
	tm := timeRunner(flushRunner(), quickConfig())
	if tm.AllocsPerOp < 30 {
		t.Errorf("published %.1f calls a run; the periodic cost makes it about 100", tm.AllocsPerOp)
	}
}

// refillRunner loses its state when a collection has happened since its last run, as a library
// does whose sync.Pool a collection emptied: the next run allocates the state again (2000 calls).
// Every 11th run ends with a collection, standing for one the program started on its own.
func refillRunner() Runner {
	return newRunner(func() struct{} { return struct{}{} },
		func(*struct{}) ([]byte, error) {
			for t0 := time.Now(); time.Since(t0) < 300*time.Microsecond; {
			}
			var ms runtime.MemStats
			runtime.ReadMemStats(&ms)
			if ms.NumGC != refillGC {
				refillGC = ms.NumGC
				refillSink = refillSink[:0]
				for i := 0; i < 2000; i++ {
					refillSink = append(refillSink, make([]byte, 16))
				}
			}
			periodRuns++
			if periodRuns%11 == 0 {
				runtime.GC()
			}
			return make([]byte, 64), nil
		}, bytesProduct)
}

// Which pass meets a collection is chance, and a stretch of runs that met one pays a refill that a
// stretch without one does not. Comparing the stretches that met none, the two passes agree on a
// library like this; comparing everything, they disagree by the luck of where the collections fell.
func TestACollectionDrivenRefillDoesNotRefuseTheCell(t *testing.T) {
	r := refillRunner()
	tm := timeRunner(r, quickConfig())
	m := memoryPass(r, avgRunsFor(tm, quickConfig()), min(tm.ItersPerSample, tm.Chunk))
	met := 0
	for _, c := range tm.Chunks {
		if c.GCs > 0 {
			met++
		}
	}
	t.Logf("%d of %d timed chunks and %d of %d groups met a collection; published %.1f calls a run, all-run memory pass %d",
		met, len(tm.Chunks), countGC(m.Groups), len(m.Groups), tm.AllocsPerOp, m.Allocations)
	if met == 0 || countGC(m.Groups) == 0 {
		t.Skip("no collection fell in the runs; nothing to show")
	}
	if d := crossCheck(tm, m, r); d != "" {
		t.Error(d)
	}
}

func countGC(g []RunStat) (n int) {
	for _, s := range g {
		if s.GCs > 0 {
			n++
		}
	}
	return n
}

// What the check compares is the stretches without a collection; a real difference between them is
// still refused, and so is one that only the stretches with a collection show.
func TestTheCrossCheckStillRefusesARealDisagreement(t *testing.T) {
	clean := RunStat{Runs: 4, Mallocs: 8, Bytes: 4 * 100}
	tm := Timing{AllocsPerOp: 2, BytesPerOp: 100, Chunks: []RunStat{clean, clean, clean}}
	ok := Memory{Allocations: 2, AllocatedBytes: 100, Groups: []RunStat{clean, clean}}
	if d := agree(tm, ok); d != "" {
		t.Fatal(d)
	}
	double := RunStat{Runs: 4, Mallocs: 32, Bytes: 4 * 100}
	bad := Memory{Allocations: 8, AllocatedBytes: 100, Groups: []RunStat{double, double}}
	if agree(tm, bad) == "" {
		t.Error("twice the allocations in the memory pass was accepted")
	}
	// A refill in the stretches that met a collection is left out of both sides.
	withGC := RunStat{Runs: 4, Mallocs: 4000, Bytes: 4 * 100, GCs: 1}
	tm2 := Timing{AllocsPerOp: 500, BytesPerOp: 100, Chunks: []RunStat{clean, withGC, clean}}
	if d := agree(tm2, ok); d != "" {
		t.Errorf("a stretch that met a collection counted: %s", d)
	}
}

// Two passes of different lengths hold a different number of a by-design periodic event, so their
// means differ by about one event. That is accepted as the uncertainty of the mean; the same gap
// between passes whose stretches all look alike is a real disagreement and is refused.
func TestOneRareEventIsTheUncertaintyOfAMean(t *testing.T) {
	plain := RunStat{Runs: 50, Mallocs: 100, Bytes: 5000}
	spike := RunStat{Runs: 50, Mallocs: 100 + 2000, Bytes: 5000 + 200000}
	rep := func(n int, s ...RunStat) []RunStat {
		out := make([]RunStat, 0, n+len(s))
		for i := 0; i < n; i++ {
			out = append(out, plain)
		}
		return append(out, s...)
	}
	// 100 stretches that hold 2 events: 2.8 calls a run; 10 stretches that hold 1: 6 calls a run.
	timed := Timing{Chunks: append(rep(98), spike, spike)}
	timed.AllocsPerOp, timed.BytesPerOp, _ = steadyMeans(timed.Chunks)
	mem := Memory{Groups: append(rep(9), spike)}
	mc, mb, _ := steadyMeans(mem.Groups)
	mem.Allocations, mem.AllocatedBytes = uint64(mc), uint64(mb)
	if d := agree(timed, mem); d != "" {
		t.Errorf("one event of difference between the passes was refused: %s", d)
	}
	// With no event in either, a gap of the same size is a disagreement.
	smooth := Timing{Chunks: rep(31)}
	smooth.AllocsPerOp, smooth.BytesPerOp, _ = steadyMeans(smooth.Chunks)
	other := RunStat{Runs: 50, Mallocs: 50 * 9, Bytes: 5000}
	m2 := Memory{Groups: []RunStat{other, other, other}}
	if agree(smooth, m2) == "" {
		t.Error("a gap with no rare event behind it was accepted")
	}
}
