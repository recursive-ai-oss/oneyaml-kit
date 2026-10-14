package main

// github.com/goccy/go-yaml, driven through its decoder for values and through its AST
// (parser.ParseBytes, the YAML Path and File.String) for documents.

import (
	"bytes"
	"fmt"
	"strings"

	goccy "github.com/goccy/go-yaml"
	"github.com/goccy/go-yaml/ast"
	"github.com/goccy/go-yaml/parser"
)

// pathText is the library's YAML Path for a resolved path: `$.a.b[0].c`, with a key quoted
// when it is not a plain word.
func pathText(segs []Seg) string {
	var b strings.Builder
	b.WriteString("$")
	for _, s := range segs {
		if s.IsIdx {
			fmt.Fprintf(&b, "[%d]", s.Index)
			continue
		}
		plain := s.Key != ""
		for _, r := range s.Key {
			if !(r == '_' || r == '-' || r >= '0' && r <= '9' || r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z') {
				plain = false
			}
		}
		if plain {
			b.WriteString("." + s.Key)
		} else {
			b.WriteString(".'" + strings.ReplaceAll(s.Key, "'", "\\'") + "'")
		}
	}
	return b.String()
}

func libGoccy() *yamlLib {
	l := &yamlLib{
		id: "goccy-go-yaml", pkg: "github.com/goccy/go-yaml",
		settings: map[string]string{
			"api":      "yaml.Unmarshal, yaml.NewDecoder for streams, yaml.Marshal; parser.ParseBytes with ParseComments for documents",
			"document": "the AST prints its own spacing for flow collections and trailing comments",
		},
		unmarshal: func(in []byte, out any) error { return goccy.Unmarshal(in, out) },
		decoder: func(in []byte) func(any) error {
			return goccy.NewDecoder(bytes.NewReader(in)).Decode
		},
		marshal:    func(v any) ([]byte, error) { return goccy.Marshal(v) },
		whyNoPatch: "no patch support: its YAML Path replaces and merges but is not RFC 6902",
	}
	l.doc = &docAPI{
		parse: func(in []byte, docs int) (any, error) {
			return parser.ParseBytes(in, parser.ParseComments)
		},
		value: func(doc any) ([]any, error) {
			var out []any
			for _, d := range doc.(*ast.File).Docs {
				var v any
				if err := goccy.NodeToValue(d.Body, &v); err != nil {
					return nil, err
				}
				out = append(out, v)
			}
			return out, nil
		},
		emit: func(doc any) ([]byte, error) { return []byte(doc.(*ast.File).String()), nil },
		prepareEdit: func(steps []EditStep) (func(any) error, error) {
			type prepared struct {
				step   EditStep
				path   *goccy.Path
				parent *goccy.Path
				key    string
				idx    int
				isIdx  bool
			}
			var ps []prepared
			for _, st := range steps {
				if len(st.Segs) == 0 {
					return nil, fmt.Errorf("an edit of the whole document")
				}
				p, err := goccy.PathString(pathText(st.Segs))
				if err != nil {
					return nil, err
				}
				pp, err := goccy.PathString(pathText(st.Segs[:len(st.Segs)-1]))
				if err != nil {
					return nil, err
				}
				last := st.Segs[len(st.Segs)-1]
				ps = append(ps, prepared{st, p, pp, last.Key, last.Index, last.IsIdx})
			}
			return func(doc any) error {
				file := doc.(*ast.File)
				for _, p := range ps {
					switch p.step.Op {
					case "replace":
						// The path API replaces a node with the node parsed from a text fragment.
						text, err := goccy.Marshal(p.step.Value)
						if err != nil {
							return err
						}
						if err := p.path.ReplaceWithReader(file, bytes.NewReader(text)); err != nil {
							return err
						}
					case "remove":
						// The path API has no removal, so the AST is edited: the parent collection
						// is found by path and the entry is taken out of it.
						parent, err := p.parent.FilterFile(file)
						if err != nil {
							return err
						}
						if err := removeFrom(parent, p.key, p.idx, p.isIdx); err != nil {
							return err
						}
					default:
						return fmt.Errorf("unsupported edit %s", p.step.Op)
					}
				}
				return nil
			}, nil
		},
	}
	return l
}

func removeFrom(parent ast.Node, key string, idx int, isIdx bool) error {
	switch n := parent.(type) {
	case *ast.MappingNode:
		for i, v := range n.Values {
			if v.Key.GetToken().Value == key {
				n.Values = append(n.Values[:i], n.Values[i+1:]...)
				return nil
			}
		}
	case *ast.SequenceNode:
		if isIdx && idx < len(n.Values) {
			n.Values = append(n.Values[:idx], n.Values[idx+1:]...)
			if idx < len(n.Entries) {
				n.Entries = append(n.Entries[:idx], n.Entries[idx+1:]...)
			}
			return nil
		}
	default:
		return fmt.Errorf("the library cannot remove from a %T", parent)
	}
	return fmt.Errorf("nothing to remove")
}
