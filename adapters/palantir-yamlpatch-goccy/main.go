// Kit adapter for github.com/palantir/pkg/yamlpatch, with its goccy back end.
//
// The library is an RFC 6902 patcher and nothing else: it takes the stream and the patch
// and returns the new bytes, so the adapter offers patches only. It reads the first
// document of a stream, so other documents are not addressable through it.
package main

import (
	"example.invalid/oneyaml-kit/gokit"
	"github.com/palantir/pkg/yamlpatch"
	"github.com/palantir/pkg/yamlpatch/goccyyamlpatcher"
)

const version = "v1.5.0"

// operations builds the library's Patch from the case. Pointers go through the library's own
// ParsePath, so a malformed pointer is rejected by the library. Numbers are given as Go
// integers or floats by their JSON form, as a Go caller writing the patch would.
func operations(args gokit.PatchArgs) (yamlpatch.Patch, error) {
	ops, err := gokit.ParsePatch(args.Patch)
	if err != nil {
		return nil, err
	}
	var p yamlpatch.Patch
	for _, o := range ops {
		op := yamlpatch.Operation{Type: o.Name}
		if o.HasPath {
			if op.Path, err = yamlpatch.ParsePath(o.Path); err != nil {
				return nil, err
			}
		}
		if o.HasFrom {
			if op.From, err = yamlpatch.ParsePath(o.From); err != nil {
				return nil, err
			}
		}
		op.Value = o.Value
		p = append(p, op)
	}
	return p, nil
}

func patch(in []byte, args gokit.PatchArgs) gokit.Result {
	if args.Format == "merge-patch" {
		return gokit.Unsupported("merge patch")
	}
	if args.Document != 0 {
		return gokit.Unsupported("the library patches the first document only")
	}
	p, err := operations(args)
	if err != nil {
		return gokit.Err("other", err.Error(), 0, 0).WithOutput(in).WithEmulated("unchanged")
	}
	out, err := goccyyamlpatcher.New().Apply(in, p)
	if err != nil {
		// The library returns no bytes when it fails; the adapter reports the original.
		return gokit.Err("other", err.Error(), 0, 0).WithOutput(in).WithEmulated("unchanged")
	}
	return gokit.OKOutput(out)
}

func main() {
	a := gokit.Adapter{
		Hello: gokit.Hello{
			Name: "palantir-yamlpatch-goccy", Version: version,
			Capabilities: []string{"patch:add", "patch:remove", "patch:replace", "patch:move", "patch:copy", "patch:test"},
			Emulates:     []string{"unchanged"},
			Info: map[string]string{
				"module":  "github.com/palantir/pkg/yamlpatch " + version,
				"backend": "goccy AST (goccyyamlpatcher.New)",
				"patch":   "RFC 6902 only; first document only; errors are plain strings",
			},
		},
		Patch: patch,
	}
	a.Serve()
}
