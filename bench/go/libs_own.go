//go:build oneyaml_go_src

package main

import (
	_ "oneyaml.dev/go"
)

// sourceRevision is set by build.sh to the checkout's commit.
var sourceRevision = "devel"

const oneyamlPackage = "oneyaml.dev/go"

func init() { localRevisions[oneyamlPackage] = sourceRevision }

func ownLibraries() []Library { return []Library{oneyamlGo{}} }

// oneyamlGo is the library under test. It takes part in no task yet; each task is added to
// Tasks and to Runner together, with the library's own entry point for it.
type oneyamlGo struct{}

func (oneyamlGo) ID() string                  { return "oneyaml-go" }
func (oneyamlGo) Package() string             { return oneyamlPackage }
func (oneyamlGo) Role() string                { return "own" }
func (oneyamlGo) Settings() map[string]string { return map[string]string{} }
func (oneyamlGo) Tasks() []Task               { return nil }
func (oneyamlGo) Runner(Task, *Workload) (Runner, string) {
	return nil, "not written yet"
}
