//go:build !oneyaml_go_src

package main

// ownLibraries is the library the kit is written for. It is built in only when the harness
// is built against a checkout of it (see build.sh); otherwise the harness measures the
// competitors alone.
func ownLibraries() []Library { return nil }
