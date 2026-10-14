// Kit adapter for github.com/goccy/go-yaml.
//
// The library has no event stream, so valid input is declined for the events operation.
// Values come from decoding into any. Replacing a value goes through the library's AST:
// the adapter writes the YAML Path that names the node and the library marshals the new
// value and replaces the node's text.
package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"

	"example.invalid/oneyaml-kit/gokit"
	yaml "github.com/goccy/go-yaml"
	"github.com/goccy/go-yaml/ast"
	"github.com/goccy/go-yaml/parser"
)

const version = "v1.19.2"

// failure converts a library error. The library reports syntax errors with the token they
// stopped at, which carries a 1-based line and column, and names the depth limit.
func failure(err error) gokit.Result {
	cat := "other"
	switch {
	case errors.Is(err, yaml.ErrExceededMaxDepth):
		cat = "limit"
	default:
		var se *yaml.SyntaxError
		if errors.As(err, &se) {
			cat = "syntax"
		}
	}
	line, col := 0, 0
	var ye yaml.Error
	if errors.As(err, &ye) {
		if tk := ye.GetToken(); tk != nil && tk.Position != nil {
			line, col = tk.Position.Line, tk.Position.Column
		}
		return gokit.Err(cat, ye.GetMessage(), line, col)
	}
	return gokit.Err(cat, err.Error(), line, col)
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
	return gokit.Unsupported("the library has no event stream")
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

// scalarText is the text of a mapping key.
func scalarText(n ast.Node) (string, bool) {
	switch k := n.(type) {
	case *ast.StringNode:
		return k.Value, true
	case *ast.IntegerNode, *ast.FloatNode, *ast.BoolNode, *ast.NullNode:
		return k.GetToken().Value, true
	}
	return "", false
}

func unwrap(n ast.Node) ast.Node {
	for {
		switch x := n.(type) {
		case *ast.AnchorNode:
			n = x.Value
		case *ast.TagNode:
			n = x.Value
		default:
			return n
		}
	}
}

func isPlainKey(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if !(r == '_' || r == '-' || r >= '0' && r <= '9' || r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z') {
			return false
		}
	}
	return true
}

// yamlPath turns a JSON Pointer into the library's YAML Path by looking at what each step
// lands on: a key of a mapping or an index of a sequence. A step the adapter cannot
// express in that syntax, or that goes through an alias, is declined.
func yamlPath(root ast.Node, toks []string) (string, string) {
	path := "$"
	cur := root
	for _, tok := range toks {
		cur = unwrap(cur)
		switch n := cur.(type) {
		case *ast.MappingNode, *ast.MappingValueNode:
			var vals []*ast.MappingValueNode
			if m, ok := n.(*ast.MappingNode); ok {
				vals = m.Values
			} else {
				vals = []*ast.MappingValueNode{n.(*ast.MappingValueNode)}
			}
			var next ast.Node
			for _, mv := range vals {
				if t, ok := scalarText(mv.Key); ok && t == tok {
					next = mv.Value
					break
				}
			}
			switch {
			case strings.Contains(tok, "'") || strings.Contains(tok, "\\"):
				return "", "a mapping key with a quote or backslash"
			case isPlainKey(tok):
				path += "." + tok
			default:
				path += ".'" + tok + "'"
			}
			if next == nil {
				return path, ""
			}
			cur = next
		case *ast.SequenceNode:
			i, err := strconv.Atoi(tok)
			if err != nil || i < 0 || fmt.Sprint(i) != tok {
				return "", "a sequence step that is not an index"
			}
			path += "[" + tok + "]"
			if i >= len(n.Values) {
				return path, ""
			}
			cur = n.Values[i]
		case *ast.AliasNode:
			return "", "a path through an alias"
		default:
			return "", "a path through a scalar"
		}
	}
	return path, ""
}

func patch(in []byte, args gokit.PatchArgs) gokit.Result {
	if args.Format == "merge-patch" {
		return gokit.Unsupported("merge patch")
	}
	ops, err := gokit.ParsePatch(args.Patch)
	if err != nil {
		return gokit.Err("patch-invalid", err.Error(), 0, 0).WithOutput(in).WithEmulated("unchanged", "error")
	}
	file, err := parser.ParseBytes(in, parser.ParseComments)
	if err != nil {
		return gokit.Err("other", err.Error(), 0, 0).WithOutput(in).WithEmulated("unchanged")
	}
	if args.Document >= len(file.Docs) {
		return gokit.Err("patch-path", "no such document", 0, 0).WithOutput(in).WithEmulated("unchanged", "error")
	}
	// The library replaces in every document of a file, so it is given the one document
	// that is being patched; the nodes are shared with the whole file.
	one := &ast.File{Docs: file.Docs[args.Document : args.Document+1]}
	for _, op := range ops {
		if op.Name != "replace" {
			if op.Name == "add" || op.Name == "remove" || op.Name == "move" || op.Name == "copy" || op.Name == "test" {
				return gokit.Unsupported("patch operation " + op.Name)
			}
			return gokit.Err("patch-invalid", fmt.Sprintf("unknown operation %q", op.Name), 0, 0).WithOutput(in).WithEmulated("unchanged", "error")
		}
		if !op.HasPath || !op.HasValue {
			return gokit.Err("patch-invalid", "replace needs a path and a value", 0, 0).WithOutput(in).WithEmulated("unchanged", "error")
		}
		toks, err := gokit.Pointer(op.Path)
		if err != nil {
			return gokit.Err("patch-invalid", err.Error(), 0, 0).WithOutput(in).WithEmulated("unchanged", "error")
		}
		if len(toks) == 0 {
			return gokit.Unsupported("a patch of the whole document")
		}
		var body ast.Node = file.Docs[args.Document].Body
		ps, why := yamlPath(body, toks)
		if why != "" {
			return gokit.Unsupported(why)
		}
		p, err := yaml.PathString(ps)
		if err != nil {
			return gokit.Err("other", err.Error(), 0, 0).WithOutput(in).WithEmulated("unchanged")
		}
		text, err := yaml.Marshal(op.Value)
		if err != nil {
			return gokit.Err("other", err.Error(), 0, 0).WithOutput(in).WithEmulated("unchanged")
		}
		if err := p.ReplaceWithReader(one, bytes.NewReader(text)); err != nil {
			return gokit.Err("other", err.Error(), 0, 0).WithOutput(in).WithEmulated("unchanged")
		}
	}
	return gokit.OKOutput([]byte(file.String())).WithEmulated("pointer")
}

func main() {
	a := gokit.Adapter{
		Hello: gokit.Hello{
			Name: "goccy-go-yaml", Version: version,
			Capabilities: []string{"events", "value", "error-category", "error-position", "patch:replace"},
			Emulates:     []string{"error", "pointer", "unchanged"},
			Info: map[string]string{
				"module": "github.com/goccy/go-yaml " + version,
				"events": "no event stream: valid input is declined; accept or reject is a generic load of every document",
				"value":  "Decoder.Decode into any, every document",
				"errors": "syntax errors carry a token position; the depth limit is named; other errors are 'other'",
				"patch":  "AST edit: Path.ReplaceWithReader; the adapter writes the YAML Path from the JSON Pointer; replace only",
			},
		},
		Events: events, Value: value, Patch: patch,
	}
	a.Serve()
}
