package main

// The go-yaml family: gopkg.in/yaml.v2, go.yaml.in/yaml/v3 and go.yaml.in/yaml/v4, and the
// Kubernetes wrapper sigs.k8s.io/yaml over yaml.v2. Each is driven through the API its
// documentation shows for the task.

import (
	"bytes"
	"errors"
	"fmt"
	"io"

	yamlv3 "go.yaml.in/yaml/v3"
	yamlv4 "go.yaml.in/yaml/v4"
	yamlv2 "gopkg.in/yaml.v2"
	sigs "sigs.k8s.io/yaml"
)

func libYAMLv2() *yamlLib {
	return &yamlLib{
		id: "yaml-v2", pkg: "gopkg.in/yaml.v2",
		settings:  map[string]string{"api": "yaml.Unmarshal, yaml.NewDecoder for streams, yaml.Marshal"},
		unmarshal: yamlv2.Unmarshal,
		decoder: func(in []byte) func(any) error {
			return yamlv2.NewDecoder(bytes.NewReader(in)).Decode
		},
		marshal:    func(v any) ([]byte, error) { return yamlv2.Marshal(v) },
		whyNoDoc:   "no document model: it decodes to values and discards comments and layout",
		whyNoPatch: "no patch support",
	}
}

func libSigs() *yamlLib {
	return &yamlLib{
		id: "sigs-yaml", pkg: "sigs.k8s.io/yaml",
		settings: map[string]string{"api": "yaml.Unmarshal, yaml.Marshal (YAML to JSON to encoding/json)",
			"stream": "reads one document"},
		unmarshal:  func(in []byte, out any) error { return sigs.Unmarshal(in, out) },
		marshal:    func(v any) ([]byte, error) { return sigs.Marshal(v) },
		whyNoDoc:   "no document model: it converts to JSON and discards comments and layout",
		whyNoPatch: "no patch support",
	}
}

// ---- yaml.v3 -------------------------------------------------------------------------------

type v3Doc []*yamlv3.Node

func libYAMLv3() *yamlLib {
	l := &yamlLib{
		id: "yaml-v3", pkg: "go.yaml.in/yaml/v3",
		settings: map[string]string{
			"api":      "yaml.Unmarshal, yaml.NewDecoder for streams, yaml.Marshal; yaml.Node for documents",
			"indent":   "the Node encoder is set to 2, the indentation of the inputs",
			"document": "yaml.Node keeps comments but not blank lines or spacing",
		},
		unmarshal: yamlv3.Unmarshal,
		decoder: func(in []byte) func(any) error {
			return yamlv3.NewDecoder(bytes.NewReader(in)).Decode
		},
		marshal:    func(v any) ([]byte, error) { return yamlv3.Marshal(v) },
		whyNoPatch: "no patch support",
	}
	ops := nodeOps[*yamlv3.Node]{
		kind: func(n *yamlv3.Node) nodeKind {
			switch n.Kind {
			case yamlv3.ScalarNode:
				return kScalar
			case yamlv3.MappingNode:
				return kMapping
			case yamlv3.SequenceNode:
				return kSequence
			case yamlv3.AliasNode:
				return kAlias
			}
			return kOther
		},
		content:    func(n *yamlv3.Node) []*yamlv3.Node { return n.Content },
		setContent: func(n *yamlv3.Node, c []*yamlv3.Node) { n.Content = c },
		text:       func(n *yamlv3.Node) string { return n.Value },
		target:     func(n *yamlv3.Node) *yamlv3.Node { return n.Alias },
		setScalar: func(n *yamlv3.Node, v any) error {
			if n.Kind != yamlv3.ScalarNode {
				return fmt.Errorf("the target is not a scalar")
			}
			// The node keeps its style and comments; the text and tag come from the library's
			// own encoding of the new value.
			if s, ok := v.(string); ok {
				n.SetString(s)
				return nil
			}
			var t yamlv3.Node
			if err := t.Encode(v); err != nil {
				return err
			}
			n.Value, n.Tag = t.Value, t.Tag
			return nil
		},
	}
	l.doc = &docAPI{
		parse: func(in []byte, docs int) (any, error) {
			if docs <= 1 {
				n := new(yamlv3.Node)
				if err := yamlv3.Unmarshal(in, n); err != nil {
					return nil, err
				}
				return v3Doc{n}, nil
			}
			dec := yamlv3.NewDecoder(bytes.NewReader(in))
			var d v3Doc
			for {
				n := new(yamlv3.Node)
				err := dec.Decode(n)
				if err != nil {
					if errors.Is(err, io.EOF) {
						return d, nil
					}
					return nil, err
				}
				d = append(d, n)
			}
		},
		value: func(doc any) ([]any, error) {
			var out []any
			for _, n := range doc.(v3Doc) {
				var v any
				if err := n.Decode(&v); err != nil {
					return nil, err
				}
				out = append(out, v)
			}
			return out, nil
		},
		emit: func(doc any) ([]byte, error) {
			var buf bytes.Buffer
			enc := yamlv3.NewEncoder(&buf)
			enc.SetIndent(2)
			for _, n := range doc.(v3Doc) {
				if err := enc.Encode(n); err != nil {
					return nil, err
				}
			}
			return buf.Bytes(), enc.Close()
		},
		prepareEdit: func(steps []EditStep) (func(any) error, error) {
			return func(doc any) error {
				d := doc.(v3Doc)
				if len(d) == 0 || len(d[0].Content) == 0 {
					return fmt.Errorf("no document")
				}
				return ops.edit(d[0].Content[0], steps)
			}, nil
		},
	}
	return l
}

// ---- yaml.v4 -------------------------------------------------------------------------------

type v4Doc []*yamlv4.Node

func libYAMLv4() *yamlLib {
	l := &yamlLib{
		id: "yaml-v4", pkg: "go.yaml.in/yaml/v4",
		settings: map[string]string{
			"api":      "yaml.Load, yaml.NewLoader for streams, yaml.Dump; yaml.Node for documents",
			"defaults": "the library's own (indent 2)",
			"document": "yaml.Node keeps comments but not blank lines or spacing",
		},
		unmarshal: func(in []byte, out any) error { return yamlv4.Load(in, out) },
		decoder: func(in []byte) func(any) error {
			ld, err := yamlv4.NewLoader(bytes.NewReader(in))
			if err != nil {
				return func(any) error { return err }
			}
			return ld.Load
		},
		marshal:    func(v any) ([]byte, error) { return yamlv4.Dump(v) },
		whyNoPatch: "no patch support",
	}
	ops := nodeOps[*yamlv4.Node]{
		kind: func(n *yamlv4.Node) nodeKind {
			switch n.Kind {
			case yamlv4.ScalarNode:
				return kScalar
			case yamlv4.MappingNode:
				return kMapping
			case yamlv4.SequenceNode:
				return kSequence
			case yamlv4.AliasNode:
				return kAlias
			}
			return kOther
		},
		content:    func(n *yamlv4.Node) []*yamlv4.Node { return n.Content },
		setContent: func(n *yamlv4.Node, c []*yamlv4.Node) { n.Content = c },
		text:       func(n *yamlv4.Node) string { return n.Value },
		target:     func(n *yamlv4.Node) *yamlv4.Node { return n.Alias },
		setScalar: func(n *yamlv4.Node, v any) error {
			if n.Kind != yamlv4.ScalarNode {
				return fmt.Errorf("the target is not a scalar")
			}
			if s, ok := v.(string); ok {
				n.SetString(s)
				return nil
			}
			var t yamlv4.Node
			if err := t.Encode(v); err != nil {
				return err
			}
			n.Value, n.Tag = t.Value, t.Tag
			return nil
		},
	}
	l.doc = &docAPI{
		parse: func(in []byte, docs int) (any, error) {
			if docs <= 1 {
				n := new(yamlv4.Node)
				if err := yamlv4.Load(in, n); err != nil {
					return nil, err
				}
				return v4Doc{n}, nil
			}
			ld, err := yamlv4.NewLoader(bytes.NewReader(in))
			if err != nil {
				return nil, err
			}
			var d v4Doc
			for {
				n := new(yamlv4.Node)
				if err := ld.Load(n); err != nil {
					if errors.Is(err, io.EOF) {
						return d, nil
					}
					return nil, err
				}
				d = append(d, n)
			}
		},
		value: func(doc any) ([]any, error) {
			var out []any
			for _, n := range doc.(v4Doc) {
				var v any
				if err := n.Decode(&v); err != nil {
					return nil, err
				}
				out = append(out, v)
			}
			return out, nil
		},
		emit: func(doc any) ([]byte, error) {
			d := doc.(v4Doc)
			if len(d) == 1 {
				return yamlv4.Dump(d[0])
			}
			var buf bytes.Buffer
			dm, err := yamlv4.NewDumper(&buf)
			if err != nil {
				return nil, err
			}
			for _, n := range d {
				if err := dm.Dump(n); err != nil {
					return nil, err
				}
			}
			return buf.Bytes(), dm.Close()
		},
		prepareEdit: func(steps []EditStep) (func(any) error, error) {
			return func(doc any) error {
				d := doc.(v4Doc)
				if len(d) == 0 || len(d[0].Content) == 0 {
					return fmt.Errorf("no document")
				}
				return ops.edit(d[0].Content[0], steps)
			}, nil
		},
	}
	return l
}
