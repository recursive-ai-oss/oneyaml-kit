//go:build !selftest

package main

// The harness's own tests use deliberately wrong and refusing libraries. They are compiled
// only with the selftest tag, never into a build that produces numbers.
const selftestBuild = false

func controlLibraries() []Library { return nil }
