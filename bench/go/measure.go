package main

import (
	"flag"
	"sync/atomic"
	"testing"
)

// The timing is the standard library's own: testing.Benchmark with b.ReportAllocs, so a cell's
// figures are ns/op, B/op and allocs/op as `go test -bench -benchmem` prints them, at its default
// benchtime. Quick mode shortens the benchtime and nothing else.

// chunk bounds how many starting states exist at once: a fast task on a small input would
// otherwise be asked for a million of them.
const chunk = 256

const quickBenchtime = "20ms"

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
}

// measure times the task. Each starting state is built with the timer stopped and the result of the
// run is dropped inside the clock, as a caller's would be.
func measure(r Runner, mode string) Measured {
	if mode == "quick" {
		setBenchtime(quickBenchtime)
	} else {
		setBenchtime("1s")
	}
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
		AllocsPerOp: res.AllocsPerOp(), Iterations: res.N, Panic: panicked}
}
