//go:build selftest

package main

// The guard must accept a correct answer and refuse a wrong one for every task, and a refused
// cell must carry no numbers. These run against the control libraries.

import "testing"

func controlCell(t *testing.T, b behaviour, task Task) map[string]any {
	t.Helper()
	w := tinyWorkload(t)
	return oneCell(control{"c", b}, task, w, quickConfig())
}

func TestControlsAreJudgedOnEveryTask(t *testing.T) {
	for _, task := range allTasks {
		right := controlCell(t, behaveCorrect, task)
		if right["status"] != "ok" {
			t.Errorf("%s: the right answer got %v: %v", task, right["status"], right["detail"])
			continue
		}
		if tm := right["time"].(map[string]any); tm["median_ns"].(float64) <= 0 {
			t.Errorf("%s: no time", task)
		}
		if _, ok := right["memory"].(map[string]any); !ok {
			t.Errorf("%s: no memory", task)
		}

		wrong := controlCell(t, behaveWrong, task)
		if wrong["status"] != "differs" {
			t.Errorf("%s: a wrong answer got %v: %v", task, wrong["status"], wrong["detail"])
		}
		if _, has := wrong["time"]; has {
			t.Errorf("%s: a wrong answer got numbers", task)
		}
		if _, has := wrong["memory"]; has {
			t.Errorf("%s: a wrong answer got memory", task)
		}
		if wrong["detail"] == "" {
			t.Errorf("%s: a wrong answer has no reason", task)
		}

		rej := controlCell(t, behaveReject, task)
		if rej["status"] != "rejects" {
			t.Errorf("%s: a refusal got %v", task, rej["status"])
		}
		if _, has := rej["time"]; has {
			t.Errorf("%s: a refusal got numbers", task)
		}

		pan := controlCell(t, behavePanic, task)
		if pan["status"] != "rejects" {
			t.Errorf("%s: a panic got %v: %v", task, pan["status"], pan["detail"])
		}
	}
}

func TestASelftestBuildSaysSo(t *testing.T) {
	if !selftestBuild {
		t.Error("a build with the selftest tag must say so")
	}
	n := 0
	for _, l := range allLibraries() {
		if l.Role() == "control" {
			n++
		}
	}
	if n != 5 {
		t.Errorf("%d controls", n)
	}
}
