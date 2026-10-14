package main

// Wall-clock and memory measurement of one cell.
//
// Time: `testing.Benchmark` drives the timing loop, once per sample. Inside it the starting
// states are built in chunks with the clock stopped, the chunk runs with the clock running,
// and the chunk is released with the clock stopped again, so set-up and release are never
// timed and the heap the states hold stays bounded. Memory is one separate pass, so
// tracking never slows the timed runs.

import (
	"flag"
	"fmt"
	"math"
	"runtime"
	"sort"
	"sync/atomic"
	"testing"
	"time"
)

// Config is the measurement plan of a mode.
type Config struct {
	Warmup       time.Duration
	Samples      int
	SampleTarget time.Duration
	// MaxPrepared bounds the heap the prepared states of one chunk hold.
	MaxPrepared int64
	MaxIters    int
}

func quickConfig() Config {
	return Config{Warmup: 2 * time.Millisecond, Samples: 5, SampleTarget: time.Millisecond,
		MaxPrepared: 64 << 20, MaxIters: 10_000}
}

func fullConfig() Config {
	return Config{Warmup: 300 * time.Millisecond, Samples: 31, SampleTarget: 50 * time.Millisecond,
		MaxPrepared: 512 << 20, MaxIters: 1_000_000}
}

// Timing is the result of the timed runs.
type Timing struct {
	ItersPerSample int
	// Chunk is how many starting states one timed call builds at once; the heap is collected
	// between chunks, so a chunk is the longest stretch of runs that shares one cold start.
	Chunk int
	// SamplesNS is the per-iteration time of each sample, sorted.
	SamplesNS []float64
	// AllocsPerOp and BytesPerOp are what testing.B counted over all samples, for the
	// agreement check with the memory pass.
	AllocsPerOp float64
	BytesPerOp  float64
	Panic       any
}

func median(sorted []float64) float64 {
	n := len(sorted)
	if n == 0 {
		return 0
	}
	if n%2 == 1 {
		return sorted[n/2]
	}
	return (sorted[n/2-1] + sorted[n/2]) / 2
}

func (t Timing) Median() float64 { return median(t.SamplesNS) }

// MAD is the median absolute deviation: the spread one slow sample cannot inflate.
func (t Timing) MAD() float64 {
	m := t.Median()
	d := make([]float64, len(t.SamplesNS))
	for i, x := range t.SamplesNS {
		d[i] = math.Abs(x - m)
	}
	sort.Float64s(d)
	return median(d)
}

func heapAlloc() uint64 {
	var m runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&m)
	return m.HeapAlloc
}

// stateBytes estimates the heap one prepared state holds, by building a few with the heap
// settled before and after.
func stateBytes(r Runner) int64 {
	const k = 3
	before := heapAlloc()
	b := r.Prepare(k)
	after := heapAlloc()
	runtime.KeepAlive(b)
	if after <= before {
		return 0
	}
	return int64(after-before) / k
}

func chunkSize(r Runner, cfg Config) int {
	sz := stateBytes(r)
	if sz <= 0 {
		return cfg.MaxIters
	}
	return int(max(1, min(int64(cfg.MaxIters), cfg.MaxPrepared/sz)))
}

var testingReady atomic.Bool

// setBenchtime sets what `testing.Benchmark` runs for: a duration, or a number of iterations
// written "500x".
func setBenchtime(v string) {
	if !testingReady.Swap(true) {
		testing.Init()
	}
	_ = flag.Set("test.benchtime", v)
}

// timeRunner takes the warm-up and the samples.
func timeRunner(r Runner, cfg Config) Timing {
	chunk := chunkSize(r, cfg)
	var panicked any
	fn := func(b *testing.B) {
		b.ReportAllocs()
		b.StopTimer()
		for left := b.N; left > 0; {
			n := min(left, chunk)
			batch := r.Prepare(n)
			runtime.GC() // collect what the previous chunk left, with the clock stopped
			b.StartTimer()
			batch.Run()
			b.StopTimer()
			if p := batch.Panic(); p != nil && panicked == nil {
				panicked = p
			}
			batch = nil
			left -= n
		}
	}
	setBenchtime(cfg.Warmup.String())
	warm := testing.Benchmark(fn)
	// The warm-up call found how long one iteration takes. Each sample then runs exactly the
	// number of iterations that fills the sample target, so every sample has the same size and
	// the framework does not ramp up again for each one.
	n := 1
	if warm.N > 0 && warm.T > 0 {
		per := float64(warm.T.Nanoseconds()) / float64(warm.N)
		n = int(math.Max(1, math.Min(float64(cfg.MaxIters), math.Round(float64(cfg.SampleTarget.Nanoseconds())/per))))
	}
	setBenchtime(fmt.Sprintf("%dx", n))
	t := Timing{Chunk: chunk}
	var totalN, totalAllocs, totalBytes uint64
	for i := 0; i < cfg.Samples && panicked == nil; i++ {
		res := testing.Benchmark(fn)
		if res.N == 0 {
			continue
		}
		t.SamplesNS = append(t.SamplesNS, float64(res.T.Nanoseconds())/float64(res.N))
		t.ItersPerSample = res.N
		totalN += uint64(res.N)
		totalAllocs += res.MemAllocs
		totalBytes += res.MemBytes
	}
	sort.Float64s(t.SamplesNS)
	if totalN > 0 {
		t.AllocsPerOp = float64(totalAllocs) / float64(totalN)
		t.BytesPerOp = float64(totalBytes) / float64(totalN)
	}
	t.Panic = panicked
	return t
}

// Memory is the result of the memory pass.
type Memory struct {
	// RetainedBytes is what one run leaves behind: the live heap one collection after the run,
	// less the live heap just before it with the starting state held. It is what a library keeps in
	// pools and caches, which the allocation figures of the steady state do not show.
	RetainedBytes  uint64
	PeakBytes      uint64
	AllocatedBytes uint64
	Allocations    uint64
}

// avgRunsFor is how many runs the allocation figures of the memory pass are averaged over:
// up to 64, fewer when the task is slow, so the pass stays short, and never more than a chunk.
func avgRunsFor(t Timing, cfg Config) int {
	const budgetNS = 25e6
	n := 64
	if m := t.Median(); m > 0 {
		n = int(math.Max(3, math.Min(64, budgetNS/m)))
	}
	return n
}

// memoryPass takes the memory figures in two runs of its own, so that tracking never slows the
// timed ones.
//
// Peak: the task runs once with the heap settled and a sampler goroutine reading the heap
// every millisecond. The starting state is built first, so it is in the peak, but what the
// process held before it was built (the workloads, the libraries' tables) is taken off. The peak
// is the highest `HeapAlloc` seen. A task shorter than the sampling interval is not seen by the
// sampler, so the reading right after the run counts too. `HeapAlloc` includes garbage not yet
// collected, so the peak is an upper bound on the live bytes at the moment of the reading.
//
// Allocation: the bytes and calls are the average of a batch of runs, because a library that
// keeps buffers in a pool allocates more in one cold run than in the steady state the timed
// runs measure. For a library without pools the average is the single run.
//
// The batch is split into groups of `group` runs, each with its own starting states and a
// collection before it, as the timed samples are. A library whose pools and caches are emptied by
// a collection allocates a few extra times in the first run after one. testing.B spreads that
// cost over the few runs of a sample, so a pass that spread it over all of its runs would read
// lower on a cell with few allocations and the two would disagree for no real reason.
func memoryPass(r Runner, runs, group int) Memory {
	group = max(1, min(group, runs))
	// One unmeasured run first, so that what a library keeps in pools and caches is in the
	// state it has in the timed runs; one collection then leaves those pools usable.
	warm := r.Prepare(1)
	warm.Run()
	warm = nil
	// What the process holds before the starting state exists (the workloads, the libraries'
	// tables) is not the task's, so the peak is counted from it.
	base := heapAlloc()
	batch := r.Prepare(1)
	runtime.GC()
	var before, after runtime.MemStats
	stop := make(chan struct{})
	done := make(chan uint64)
	started := make(chan struct{})
	tick := time.NewTicker(time.Millisecond)
	var sampled runtime.MemStats
	go func() {
		var peak uint64
		close(started)
		for {
			select {
			case <-stop:
				done <- peak
				return
			case <-tick.C:
				runtime.ReadMemStats(&sampled)
				if sampled.HeapAlloc > peak {
					peak = sampled.HeapAlloc
				}
			}
		}
	}()
	<-started
	runtime.ReadMemStats(&before)
	batch.Run()
	runtime.ReadMemStats(&after)
	close(stop)
	peak := <-done
	tick.Stop()
	peak = max(peak, after.HeapAlloc, before.HeapAlloc)
	var kept runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&kept)
	var retained uint64
	if kept.HeapAlloc > before.HeapAlloc {
		retained = kept.HeapAlloc - before.HeapAlloc
	}
	if peak > base {
		peak -= base
	} else {
		peak = 0
	}
	runtime.KeepAlive(batch)
	batch = nil

	var allocBytes, mallocs, total uint64
	for left := runs; left > 0; {
		k := min(left, group)
		avg := r.Prepare(k)
		runtime.GC()
		runtime.ReadMemStats(&before)
		avg.Run()
		runtime.ReadMemStats(&after)
		runtime.KeepAlive(avg)
		allocBytes += after.TotalAlloc - before.TotalAlloc
		mallocs += after.Mallocs - before.Mallocs
		total += uint64(k)
		left -= k
	}
	return Memory{RetainedBytes: retained, PeakBytes: peak,
		AllocatedBytes: (allocBytes + total/2) / total,
		Allocations:    (mallocs + total/2) / total}
}

// agree checks the memory pass against the allocation counts testing.B took in the timed
// runs, which are averages over many more runs. The number of calls must agree within 5%; the
// bytes within 25%, because a library that keeps buffers in a pool allocates fewer bytes the
// longer the heap stays unchanged, and the two runs do not see the same number of collections.
// A few bytes and calls of slack cover rounding and the runtime's own bookkeeping. A larger
// difference means a figure cannot be trusted and the cell gets no numbers.
func agree(t Timing, m Memory) string {
	if t.AllocsPerOp == 0 && t.BytesPerOp == 0 {
		return ""
	}
	const slackCalls, slackBytes = 2.0, 128.0
	okf := func(a, b, tol, slack float64) bool { return math.Abs(a-b) <= math.Max(tol*math.Max(a, b), slack) }
	if !okf(t.AllocsPerOp, float64(m.Allocations), 0.05, slackCalls) || !okf(t.BytesPerOp, float64(m.AllocatedBytes), 0.25, slackBytes) {
		return fmt.Sprintf("the memory pass (%d allocations, %d bytes) and testing.B (%.1f allocations, %.0f bytes per op) disagree",
			m.Allocations, m.AllocatedBytes, t.AllocsPerOp, t.BytesPerOp)
	}
	return ""
}
