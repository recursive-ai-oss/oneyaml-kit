//go:build oneyaml_go_src

// Kit adapter for oneyaml-go, built against a checkout named by KIT_ONEYAML_GO_SRC.
//
// It declares no capabilities yet, so the kit sends it no case and counts every case as
// unsupported. Each operation below is a seam: a function that is nil in the Adapter today
// and says what it will call once the library offers it. Declaring a capability and
// filling its function go together, and nowhere else changes.
package main

import (
	"example.invalid/oneyaml-kit/gokit"
	// The library under test. Linking it here proves the build wiring; the operations
	// below will call into it.
	_ "oneyaml.dev/go"
)

// version is set by build.sh to the checkout's commit.
var version = "devel"

// events will run the library's event parser over every document and answer with the
// kit's event lines (gokit.OKEvents) or the library's error with its category and
// position (gokit.Err). Declare "events", "error-events", "error-category" and
// "error-position" with it.
//
// func events(in []byte) gokit.Result

// value will load every document into generic values and convert them with
// gokit.Documents (gokit.OKValue), or answer gokit.Err. Declare "value" with it.
//
// func value(in []byte) gokit.Result

// patch will parse the input as a lossless document, apply args.Patch (RFC 6902, RFC
// 7396 or the edit forms the case names) and answer the whole stream with
// gokit.OKOutput, or gokit.Err when the library refuses the patch. Declare the
// "patch:<op>" capabilities it supports.
//
// func patch(in []byte, args gokit.PatchArgs) gokit.Result

func main() {
	a := gokit.Adapter{
		Hello: gokit.Hello{
			Name: "oneyaml-go", Version: version,
			Capabilities: []string{},
			Emulates:     []string{},
			Info: map[string]string{
				"module": "oneyaml.dev/go " + version,
				"source": "a checkout named by KIT_ONEYAML_GO_SRC; no operation is wired yet",
			},
		},
		// Events: events, Value: value, Patch: patch,
	}
	a.Serve()
}
