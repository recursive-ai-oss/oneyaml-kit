//go:build oneyaml_go_src

package main

// oneyaml.dev/go, built from a checkout named by KIT_ONEYAML_GO_SRC (see build.sh). It is
// driven through its public API only: Unmarshal and Decoder for values, Marshal for
// dumping, and Parse for the lossless document (Bytes to write it out, Set and Remove for
// edits).

import (
	"bytes"
	"fmt"
	"strconv"
	"strings"

	"oneyaml.dev/go"
)

// sourceRevision is set by build.sh to the checkout's commit.
var sourceRevision = "devel"

const oneyamlPackage = "oneyaml.dev/go"

func init() { localRevisions[oneyamlPackage] = sourceRevision }

func ownLibraries() []Library { return []Library{libOneyamlGo()} }

// ownLib is the library under test: a yamlLib whose role is "own".
type ownLib struct{ *yamlLib }

func (ownLib) Role() string { return "own" }

// pointer writes path segments as an RFC 6901 pointer.
func pointer(segs []Seg) string {
	var b strings.Builder
	for _, s := range segs {
		b.WriteByte('/')
		if s.IsIdx {
			b.WriteString(strconv.Itoa(s.Index))
			continue
		}
		b.WriteString(strings.NewReplacer("~", "~0", "/", "~1").Replace(s.Key))
	}
	return b.String()
}

func libOneyamlGo() Library {
	l := &yamlLib{
		id: "oneyaml-go", pkg: oneyamlPackage,
		settings: map[string]string{
			"api":      "Unmarshal, NewDecoder for streams, Marshal; Parse for documents (Bytes, Set, Remove, Apply)",
			"defaults": "the library's own (indent 2, default limits)",
			"document": "lossless: an unedited document returns the source text it kept, without rendering; edits keep comments and style",
		},
		unmarshal: func(in []byte, out any) error { return oneyaml.Unmarshal(in, out) },
		decoder: func(in []byte) func(any) error {
			return oneyaml.NewDecoder(bytes.NewReader(in)).Decode
		},
		marshal: func(v any) ([]byte, error) { return oneyaml.Marshal(v) },
	}
	l.patch = &patchAPI{
		prepare: func(ops []Op) (any, error) {
			out := make([]oneyaml.PatchOp, 0, len(ops))
			for _, o := range ops {
				out = append(out, oneyaml.PatchOp{Op: o.Op, Path: o.Path, Value: native(o.Value)})
			}
			return out, nil
		},
		apply: func(in []byte, p any) ([]byte, error) {
			d, err := oneyaml.Parse(in)
			if err != nil {
				return nil, err
			}
			if err := d.Apply(p.([]oneyaml.PatchOp)...); err != nil {
				return nil, err
			}
			return d.Bytes(), nil
		},
	}
	l.doc = &docAPI{
		parse: func(in []byte, docs int) (any, error) { return oneyaml.Parse(in) },
		// The values come from the document's own nodes, one decode per document, so the guard
		// judges the parsed tree and not the text it kept.
		value: func(doc any) ([]any, error) {
			d := doc.(*oneyaml.Document)
			var out []any
			for i := 0; i < d.Len(); i++ {
				var v any
				if err := d.Doc(i).Decode(&v); err != nil {
					return nil, err
				}
				out = append(out, v)
			}
			return out, nil
		},
		emit: func(doc any) ([]byte, error) { return doc.(*oneyaml.Document).Bytes(), nil },
		prepareEdit: func(steps []EditStep) (func(any) error, error) {
			type prepared struct {
				op, ptr string
				value   any
			}
			var ps []prepared
			for _, st := range steps {
				if st.Op != "replace" && st.Op != "remove" {
					return nil, fmt.Errorf("unsupported edit %s", st.Op)
				}
				ps = append(ps, prepared{st.Op, pointer(st.Segs), st.Value})
			}
			return func(doc any) error {
				d := doc.(*oneyaml.Document)
				for _, p := range ps {
					var err error
					if p.op == "remove" {
						err = d.Remove(p.ptr)
					} else {
						err = d.Set(p.ptr, p.value)
					}
					if err != nil {
						return err
					}
				}
				return nil
			}, nil
		},
	}
	return ownLib{l}
}
