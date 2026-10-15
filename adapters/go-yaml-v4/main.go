// Kit adapter for go.yaml.in/yaml/v4 (release candidate; the classic Decoder API).
//
// The library has no event stream, so the events operation is answered "accepted" or with
// the library's own error. Values come from
// decoding into any. Edits go through the library's yaml.Node: the adapter finds the node a
// JSON Pointer names and the library encodes the value and writes the tree.
package main

import (
	"bytes"
	"errors"
	"io"
	"regexp"

	"example.invalid/oneyaml-kit/gokit"
	yaml "go.yaml.in/yaml/v4"
)

const version = "v4.0.0-rc.6"

var duplicateKeyMessage = regexp.MustCompile(`\Amapping key .* already defined at line \d+\z`)

// failure converts a load error. The library reports the stage that failed and a 1-based
// position, so those are passed on as they are; what it does not name (a limit, say) stays
// "other".
func failure(err error) gokit.Result {
	var le *yaml.LoadError
	if !errors.As(err, &le) {
		return gokit.Err("other", err.Error(), 0, 0)
	}
	cat := "other"
	switch le.Stage {
	case "reader":
		cat = "encoding"
	case "scanner", "parser":
		cat = "syntax"
	case "constructor":
		// A repeated key is a construction error with no code of its own, so the message is the
		// only signal; it is matched in full.
		if duplicateKeyMessage.MatchString(le.Message) {
			cat = "duplicate-key"
		}
	}
	return gokit.Err(cat, le.Message, le.Mark.Line, le.Mark.Column)
}

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
		return failure(err)
	}
	return gokit.Accepted()
}

func value(in []byte) gokit.Result {
	docs, err := decodeAll(in)
	if err != nil {
		return failure(err)
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

func scalarNode(v any) (*yaml.Node, error) {
	var n yaml.Node
	err := n.Encode(v)
	return &n, err
}

// build makes a node for a patch value: the library encodes scalars and the adapter strings
// the children together in the patch's key order.
func build(v any) (*yaml.Node, error) {
	return gokit.Build(v, scalarNode,
		func(c []*yaml.Node) *yaml.Node { return &yaml.Node{Kind: yaml.MappingNode, Tag: "!!map", Content: c} },
		func(c []*yaml.Node) *yaml.Node { return &yaml.Node{Kind: yaml.SequenceNode, Tag: "!!seq", Content: c} })
}

// set puts a value into an existing node the way a user editing the tree would. A scalar
// that becomes a scalar keeps its own style and comments and takes the text and tag the
// library's encoder gives the new value, so the encoder then picks the block header or
// quoting the text needs. Anything else is replaced by the freshly built node.
func set(dst *yaml.Node, v any) error {
	n, err := build(v)
	if err != nil {
		return err
	}
	if dst.Kind == yaml.ScalarNode && n.Kind == yaml.ScalarNode {
		dst.Value, dst.Tag = n.Value, n.Tag
		return nil
	}
	*dst = *n
	return nil
}

var tree = gokit.Tree[*yaml.Node]{
	Kind:       kind,
	Content:    func(n *yaml.Node) []*yaml.Node { return n.Content },
	SetContent: func(n *yaml.Node, c []*yaml.Node) { n.Content = c },
	Text:       func(n *yaml.Node) string { return n.Value },
	Target:     func(n *yaml.Node) *yaml.Node { return n.Alias },
	Set:        set,
	New:        build,
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
	return gokit.Finish(in, f, un, len(ops) > 0, func() ([]byte, error) {
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

// dump writes the value with the library's ordinary Marshal and default settings; the text is
// returned as it is.
func dump(v any) gokit.Result {
	b, err := yaml.Marshal(v)
	if err != nil {
		return gokit.Err("other", err.Error(), 0, 0)
	}
	return gokit.OKOutput(b)
}

func main() {
	a := gokit.Adapter{
		Hello: gokit.Hello{
			Name: "go-yaml-v4", Version: version,
			Capabilities: []string{"outcome:events", "value", "error-category", "error-position", "patch:add", "patch:remove", "patch:replace", "dump"},
			Emulates:     []string{"error", "pointer", "unchanged"},
			Info: map[string]string{
				"dump":    "yaml.Marshal, default settings",
				"module":  "go.yaml.in/yaml/v4 " + version,
				"events":  "no event stream: accept or reject is a generic load of every document",
				"value":   "Decoder.Decode into any, every document",
				"errors":  "LoadError stage and mark; the stage names the category where it can",
				"patch":   "yaml.Node tree edit; the adapter resolves JSON Pointers (first match, aliases followed)",
				"encoder": "indent 2",
			},
		},
		Events: events, Value: value, Patch: patch, Dump: dump,
	}
	a.Serve()
}
