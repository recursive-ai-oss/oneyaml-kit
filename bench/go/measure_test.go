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
	if m := memoryPass(leaky, 3); m.RetainedBytes < 1<<19 {
		t.Errorf("a run that keeps a megabyte retained %d bytes", m.RetainedBytes)
	}
	keep = nil
	clean := newRunner(func() struct{} { return struct{}{} },
		func(*struct{}) ([]byte, error) { return make([]byte, 1<<20), nil },
		bytesProduct)
	if m := memoryPass(clean, 3); m.RetainedBytes > 1<<18 {
		t.Errorf("a run that keeps nothing retained %d bytes", m.RetainedBytes)
	}
}
