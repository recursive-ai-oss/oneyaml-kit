package main

import (
	"strings"
	"testing"
)

// The harness record states the settings of the mode it ran in, not those of the other mode.
func TestHarnessRecordNamesTheBenchtimeOfItsMode(t *testing.T) {
	for mode, want := range map[string]string{"quick": "20ms", "full": "1s"} {
		rec := harnessRecord(&args{mode: mode}, nil)
		timer, _ := rec["timer"].(string)
		if !strings.HasSuffix(timer, "benchtime "+want) {
			t.Errorf("%s: timer %q does not end with the benchtime %s", mode, timer, want)
		}
		if strings.Contains(timer, "quick mode") {
			t.Errorf("%s: timer %q describes another mode", mode, timer)
		}
	}
}

// Every library's patch is timed from text to text; the record says so.
func TestHarnessRecordNamesTheTextTasks(t *testing.T) {
	rec := harnessRecord(&args{mode: "quick"}, nil)
	tasks, _ := rec["text_tasks"].([]string)
	if len(tasks) != 1 || tasks[0] != "patch" {
		t.Errorf("text_tasks = %v, want [patch]", tasks)
	}
}
