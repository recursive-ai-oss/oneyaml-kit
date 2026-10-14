package main

import (
	"strings"
	"testing"
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

// testing.B ran two runs per sample, so it saw (2*40+6)/2 = 43 allocations per run. A memory pass
// that averages 64 runs after one cold start reads 40 and the check refuses the cell, though both
// are right about the library. Grouping the runs as the samples were gives the same figure.
func TestMemoryPassSeesTheColdStartTheSamplesSaw(t *testing.T) {
	timed := Timing{ItersPerSample: 2, Chunk: 2, AllocsPerOp: 43, BytesPerOp: 43 * 112}

	flat := memoryPass(coldStartRunner(), 64, 64)
	if d := agree(timed, flat); d == "" {
		t.Errorf("without grouping the figures should disagree here: %+v", flat)
	}
	grouped := memoryPass(coldStartRunner(), 64, 2)
	if d := agree(timed, grouped); d != "" {
		t.Errorf("grouped like the samples, the figures must agree: %v (%+v)", d, grouped)
	}
	if grouped.Allocations < 42 || grouped.Allocations > 44 {
		t.Errorf("expected about 43 allocations a run, got %d", grouped.Allocations)
	}
}

// Grouping must not hide a real difference: a library that allocates twice as much in the timed
// runs as in the memory pass is still refused.
func TestGroupedMemoryPassStillRefusesARealDisagreement(t *testing.T) {
	timed := Timing{ItersPerSample: 2, Chunk: 2, AllocsPerOp: 86, BytesPerOp: 86 * 112}
	m := memoryPass(coldStartRunner(), 64, 2)
	if agree(timed, m) == "" {
		t.Errorf("a doubled allocation count was accepted: %+v", m)
	}
}
