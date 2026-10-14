// Kit adapter for go.yaml.in/yaml/v3 (the module gopkg.in/yaml.v3 was renamed to).
//
// The library has no event stream, so valid input is answered "unsupported" for the
// events operation, while rejection of invalid input is the library's own. Values come from
// decoding into any. Edits go through the library's yaml.Node: the adapter finds the node a
// JSON Pointer names and the library encodes the value and writes the tree.
package main

import (
	"bytes"
	"errors"
	"io"

	"example.invalid/oneyaml-kit/gokit"
	yaml "go.yaml.in/yaml/v3"
)

const version = "v3.0.5"

func decodeAll(in []byte) ([]any, error) {
	dec := yaml.NewDecoder(bytes.NewReader(in))
	var docs []any
	for {
		var v any
		err := dec.Decode(&v)
		if errors.Is(err, io.EOF) {
			return docs, nil
		}
		if err != nil {
			return nil, err
		}
		docs = append(docs, v)
	}
}

func events(in []byte) gokit.Result {
	if _, err := decodeAll(in); err != nil {
		return gokit.Err("other", err.Error(), 0, 0)
	}
	return gokit.Unsupported("the library has no event stream")
}

func value(in []byte) gokit.Result {
	docs, err := decodeAll(in)
	if err != nil {
		return gokit.Err("other", err.Error(), 0, 0)
	}
	out, err := gokit.Documents(docs)
	if err != nil {
		return gokit.Unsupported(err.Error())
	}
	return gokit.OKValue(out)
}

func kind(n *yaml.Node) gokit.Kind {
	switch n.Kind {
	case yaml.MappingNode:
		return gokit.Mapping
	case yaml.SequenceNode:
		return gokit.Sequence
	case yaml.AliasNode:
		return gokit.Alias
	}
	return gokit.Scalar
}

const quoted = yaml.LiteralStyle | yaml.FoldedStyle | yaml.DoubleQuotedStyle | yaml.SingleQuotedStyle

// set puts a value into an existing node. A string replacing a scalar that is quoted or a
// block scalar keeps that style, which is how the library is used to edit a value in place;
// anything else is encoded afresh by the library and keeps only the comments.
func set(dst *yaml.Node, v any) error {
	if s, ok := v.(string); ok && dst.Kind == yaml.ScalarNode && dst.Style&quoted != 0 {
		dst.Value, dst.Tag = s, "!!str"
		return nil
	}
	var n yaml.Node
	if err := n.Encode(v); err != nil {
		return err
	}
	n.HeadComment, n.LineComment, n.FootComment = dst.HeadComment, dst.LineComment, dst.FootComment
	*dst = n
	return nil
}

var tree = gokit.Tree[*yaml.Node]{
	Kind:       kind,
	Content:    func(n *yaml.Node) []*yaml.Node { return n.Content },
	SetContent: func(n *yaml.Node, c []*yaml.Node) { n.Content = c },
	Text:       func(n *yaml.Node) string { return n.Value },
	Target:     func(n *yaml.Node) *yaml.Node { return n.Alias },
	Set:        set,
	New: func(v any) (*yaml.Node, error) {
		var n yaml.Node
		err := n.Encode(v)
		return &n, err
	},
}

func patch(in []byte, args gokit.PatchArgs) gokit.Result {
	if args.Format == "merge-patch" {
		return gokit.Unsupported("merge patch")
	}
	ops, err := gokit.ParsePatch(args.Patch)
	if err != nil {
		return gokit.Err("patch-invalid", err.Error(), 0, 0).WithOutput(in).WithEmulated("unchanged", "error")
	}
	dec := yaml.NewDecoder(bytes.NewReader(in))
	var docs []*yaml.Node
	for {
		var n yaml.Node
		err := dec.Decode(&n)
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return gokit.Err("other", err.Error(), 0, 0).WithOutput(in).WithEmulated("unchanged")
		}
		docs = append(docs, &n)
	}
	if args.Document >= len(docs) || len(docs[args.Document].Content) == 0 {
		return gokit.Err("patch-path", "no such document", 0, 0).WithOutput(in).WithEmulated("unchanged", "error")
	}
	f, un := tree.Apply(docs[args.Document].Content[0], ops)
	return gokit.Finish(in, f, un, func() ([]byte, error) {
		var buf bytes.Buffer
		enc := yaml.NewEncoder(&buf)
		enc.SetIndent(2)
		for _, d := range docs {
			if err := enc.Encode(d); err != nil {
				return nil, err
			}
		}
		err := enc.Close()
		return buf.Bytes(), err
	})
}

func main() {
	a := gokit.Adapter{
		Hello: gokit.Hello{
			Name: "go-yaml-v3", Version: version,
			Capabilities: []string{"events", "value", "patch:add", "patch:remove", "patch:replace"},
			Emulates:     []string{"error", "pointer", "unchanged"},
			Info: map[string]string{
				"module":  "go.yaml.in/yaml/v3 " + version,
				"events":  "no event stream: valid input is declined; accept or reject is a generic load of every document",
				"value":   "Decoder.Decode into any, every document",
				"errors":  "plain error strings: no category and no position are reported",
				"patch":   "yaml.Node tree edit; the adapter resolves JSON Pointers",
				"encoder": "indent 2",
			},
		},
		Events: events, Value: value, Patch: patch,
	}
	a.Serve()
}
