//go:build selftest

package main

// A build with the selftest tag carries the control libraries and says so in its record, so a
// result from it can never be published.
const selftestBuild = true

func controlLibraries() []Library {
	return []Library{control{"control-correct", behaveCorrect}, control{"control-wrong", behaveWrong},
		control{"control-reject", behaveReject}, control{"control-panic", behavePanic},
		control{"control-crash", behaveCrash}}
}
