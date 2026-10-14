//go:build !oneyaml_go_src

// Without the library this module still compiles, so formatting, vet and tests run on a
// machine that has no checkout of it. build.sh adds the tag that selects main.go.
package main

import (
	"fmt"
	"os"
)

func main() {
	fmt.Fprintln(os.Stderr, "oneyaml-go adapter: build it with build.sh and KIT_ONEYAML_GO_SRC set")
	os.Exit(2)
}
