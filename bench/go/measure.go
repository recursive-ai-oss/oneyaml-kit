package main

import (
	"flag"
	"fmt"
	"sync/atomic"
	"testing"
	"time"
)

// The timing is the standard library's own: testing.Benchmark with b.ReportAllocs, so a cell's
// figures are ns/op, B/op and allocs/op as `go test -bench -benchmem` prints them, at its default
// benchtime. Quick mode shortens the benchtime and nothing else.

// chunk bounds how many starting states exist at once: a fast task on a small input would
// otherwise be asked for a million of them.
const chunk = 256

const quickBenchtime = "20ms"

// fullBenchtime is the standard library's own default.
const fullBenchtime = "1s"

// benchtimeFor is the -benchtime a mode runs with; the harness record states it too.
func benchtimeFor(mode string) string {
	if mode == "quick" {
		return quickBenchtime
	}
	return fullBenchtime
}

// setupBudgetFactor bounds the time spent building starting states, as a multiple of the benchtime.
// Every iteration has a state of its own, so a task that takes nanoseconds on a state that takes
// hundreds of microseconds to build would be asked for a million states by the standard library's
// time-based loop. When the states would cost more than this, the cell runs a fixed number of
// iterations that fits the budget instead, and the harness record says so.
const setupBudgetFactor = 50

// minFixedIterations keeps a budget-limited cell from being a handful of runs.
const minFixedIterations = 64

var testingReady atomic.Bool

func setBenchtime(v string) {
	if !testingReady.Swap(true) {
		testing.Init()
	}
	_ = flag.Set("test.benchtime", v)
}

// Measured is what testing.Benchmark reports for a cell.
type Measured struct {
	NsPerOp     float64
	BytesPerOp  int64
	AllocsPerOp int64
	Iterations  int
	Panic       any
	// Fixed says the cell ran a fixed number of iterations because building its states would have
	// exceeded the setup budget.
	Fixed bool
	// Probe is what that decision was made on, kept so it can be audited.
	Probe probeResult
}

// probeResult is the cost per state of building one and of running one, from the small batch the
// harness timed before choosing how to measure, and the iterations the setup budget allowed.
type probeResult struct {
	SetupNs, RunNs   float64
	BudgetIterations int
}

// measure times the task. Each starting state is built with the timer stopped and the result of the
// run is dropped inside the clock, as a caller's would be.
func measure(r Runner, mode string) Measured {
	benchtime := benchtimeFor(mode)
	fixed, probe := fixedIterations(r, benchtime)
	if fixed > 0 {
		benchtime = fmt.Sprintf("%dx", fixed)
	}
	setBenchtime(benchtime)
	var panicked any
	res := testing.Benchmark(func(b *testing.B) {
		b.ReportAllocs()
		for left := b.N; left > 0 && panicked == nil; {
			n := min(left, chunk)
			b.StopTimer()
			batch := r.Prepare(n)
			b.StartTimer()
			batch.Run()
			b.StopTimer()
			if p := batch.Panic(); p != nil {
				panicked = p
			}
			left -= n
		}
	})
	return Measured{NsPerOp: float64(res.T.Nanoseconds()) / float64(max(res.N, 1)), BytesPerOp: res.AllocedBytesPerOp(),
		AllocsPerOp: res.AllocsPerOp(), Iterations: res.N, Panic: panicked, Fixed: fixed > 0, Probe: probe}
}

// fixedIterations is 0 when the time-based loop fits the setup budget, else the number of
// iterations that does. It times one small batch of states being built and run: that is the
// harness choosing how to measure, not an assertion about speed.
func fixedIterations(r Runner, benchtime string) (int, probeResult) {
	const probe = 16
	t0 := time.Now()
	batch := r.Prepare(probe)
	setup := time.Since(t0)
	t1 := time.Now()
	batch.Run()
	run := time.Since(t1)
	if batch.Panic() != nil || setup <= 0 {
		return 0, probeResult{}
	}
	bt, err := time.ParseDuration(benchtime)
	if err != nil {
		return 0, probeResult{}
	}
	// The loop needs about benchtime / run iterations, and its ramp-up roughly doubles that.
	wanted := 2 * float64(bt) / (float64(max(run, 1)) / probe)
	budget := float64(setupBudgetFactor) * float64(bt)
	perState := float64(setup) / probe
	pr := probeResult{SetupNs: perState, RunNs: float64(run) / probe}
	if wanted*perState <= budget {
		return 0, pr
	}
	pr.BudgetIterations = max(int(budget/perState), minFixedIterations)
	return pr.BudgetIterations, pr
}
