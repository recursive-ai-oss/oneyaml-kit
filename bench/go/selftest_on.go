//go:build selftest

package main

// The control libraries are deliberately wrong or refusing. They exist only in a build with the
// selftest tag, never in one that produces numbers.
func controlLibraries() []Library {
	return []Library{control{"control-correct", behaveCorrect}, control{"control-wrong", behaveWrong},
		control{"control-reject", behaveReject}, control{"control-panic", behavePanic},
		control{"control-crash", behaveCrash}}
}
