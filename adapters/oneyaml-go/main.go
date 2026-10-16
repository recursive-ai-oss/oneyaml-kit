//go:build oneyaml_go_src

// Kit adapter for oneyaml-go, built against a checkout named by KIT_ONEYAML_GO_SRC.
//
// Events come from walking the lossless Document's Node tree. Values come from the
// Decoder into any. Errors carry the library's category and position. Patches go through
// Document.ApplyPatch and ApplyMergePatch, which are atomic, so nothing is emulated.
package main

import (
	"bytes"
	"errors"
	"io"
	"iter"
	"math/big"
	"strings"

	"example.invalid/oneyaml-kit/gokit"
	oneyaml "oneyaml.dev/go"
)

// version is set by build.sh to the checkout's commit.
var version = "devel"

// fail turns a library error into the protocol's error answer. The category is the
// library's own, mapped to the kit's names; anything else is "other".
func fail(err error) gokit.Result {
	var se *oneyaml.SyntaxError
	var le *oneyaml.LimitError
	switch {
	case errors.As(err, &se):
		cat := "syntax"
		switch se.Category {
		case "encoding", "duplicate-key":
			cat = string(se.Category)
		}
		return gokit.Err(cat, se.Error(), se.Line, se.Column)
	case errors.As(err, &le):
		return gokit.Err("limit", le.Error(), 0, 0)
	}
	return gokit.Err("other", err.Error(), 0, 0)
}

// value loads every document into generic values and converts them.
func value(in []byte) gokit.Result {
	dec := oneyaml.NewDecoder(bytes.NewReader(in))
	var docs []any
	for {
		var v any
		err := dec.Decode(&v)
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return fail(err)
		}
		docs = append(docs, v)
	}
	out, err := gokit.Documents(docs)
	if err != nil {
		return gokit.Unsupported(err.Error())
	}
	return gokit.OKValue(out)
}

// events walks the Document's nodes and writes the kit's event lines.
func events(in []byte) gokit.Result {
	d, err := oneyaml.Parse(in)
	if err != nil {
		return fail(err)
	}
	lines := []string{"+STR"}
	for i := 0; i < d.Len(); i++ {
		doc := d.Doc(i)
		if doc.ExplicitStart() {
			lines = append(lines, "+DOC ---")
		} else {
			lines = append(lines, "+DOC")
		}
		if root := doc.Root(); root.IsValid() {
			lines = walk(lines, root)
		} else {
			// A document with no root node is the spec's empty node: a plain empty scalar.
			lines = append(lines, "=VAL :")
		}
		if doc.ExplicitEnd() {
			lines = append(lines, "-DOC ...")
		} else {
			lines = append(lines, "-DOC")
		}
	}
	return gokit.OKEvents(append(lines, "-STR"))
}

// frame is a collection being walked. The children come from the node's iterators,
// pulled one at a time so that the walk keeps its own stack and depth is not a limit.
type frame struct {
	close string
	next  func() (oneyaml.Node, bool)
	stop  func()
	val   oneyaml.Node // the value of the entry whose key was just visited
	hasV  bool
	pairs func() (oneyaml.Node, oneyaml.Node, bool)
}

func walk(lines []string, root oneyaml.Node) []string {
	var stack []frame
	visit := func(n oneyaml.Node) {
		var p string
		if a := n.Anchor(); a != "" {
			p += " &" + a
		}
		if t, ok := n.ExplicitTag(); ok {
			p += " <" + t + ">"
		}
		switch n.Kind() {
		case oneyaml.MappingNode, oneyaml.SequenceNode:
			f := frame{}
			open := "+SEQ"
			f.close = "-SEQ"
			if n.Kind() == oneyaml.MappingNode {
				open, f.close = "+MAP", "-MAP"
				f.pairs, f.stop = iter.Pull2(n.Entries())
			} else {
				f.next, f.stop = iter.Pull(n.Items())
			}
			if n.Style() == oneyaml.FlowStyle {
				if n.Kind() == oneyaml.MappingNode {
					open += " {}"
				} else {
					open += " []"
				}
			}
			lines = append(lines, open+p)
			stack = append(stack, f)
		case oneyaml.AliasNode:
			lines = append(lines, "=ALI"+p+" *"+n.AliasName())
		default:
			lines = append(lines, "=VAL"+p+" "+styleMark(n.Style())+escape(n.Value()))
		}
	}
	visit(root)
	for len(stack) > 0 {
		f := &stack[len(stack)-1]
		var child oneyaml.Node
		var ok bool
		switch {
		case f.hasV:
			child, ok, f.hasV = f.val, true, false
		case f.pairs != nil:
			var k, v oneyaml.Node
			if k, v, ok = f.pairs(); ok {
				child, f.val, f.hasV = k, v, true
			}
		default:
			child, ok = f.next()
		}
		if !ok {
			f.stop()
			lines = append(lines, f.close)
			stack = stack[:len(stack)-1]
			continue
		}
		visit(child)
	}
	return lines
}

func styleMark(s oneyaml.NodeStyle) string {
	switch s {
	case oneyaml.SingleQuotedStyle:
		return "'"
	case oneyaml.DoubleQuotedStyle:
		return "\""
	case oneyaml.LiteralStyle:
		return "|"
	case oneyaml.FoldedStyle:
		return ">"
	}
	return ":"
}

var escaper = strings.NewReplacer("\\", "\\\\", "\x00", "\\0", "\b", "\\b", "\n", "\\n", "\r", "\\r", "\t", "\\t")

func escape(s string) string { return escaper.Replace(s) }

// patchCategory maps the library's refusal to the kit's names.
func patchCategory(err error) string {
	var le *oneyaml.LimitError
	switch {
	case errors.As(err, &le):
		return "limit"
	case errors.Is(err, oneyaml.ErrNotFound):
		return "patch-path"
	case errors.Is(err, oneyaml.ErrTestFailed):
		return "patch-test"
	case errors.Is(err, oneyaml.ErrInvalidPatch):
		return "patch-invalid"
	case errors.Is(err, oneyaml.ErrAliasWrite), errors.Is(err, oneyaml.ErrAnchorInUse):
		return "refused-alias"
	case errors.Is(err, oneyaml.ErrAmbiguousKey):
		return "refused-ambiguous"
	}
	return "other"
}

// patch applies an RFC 6902 or RFC 7396 patch with the library's own engine. The library
// is atomic, so after a refusal the document's bytes are what Bytes returns.
func patch(in []byte, args gokit.PatchArgs) gokit.Result {
	d, err := oneyaml.Parse(in)
	if err != nil {
		return fail(err).WithOutput(in)
	}
	doc := d.Doc(args.Document)
	if args.Format == "merge-patch" {
		err = doc.ApplyMergePatch(args.Patch)
	} else {
		err = doc.ApplyPatch(args.Patch)
	}
	if err != nil {
		return gokit.Err(patchCategory(err), err.Error(), 0, 0).WithOutput(d.Bytes())
	}
	return gokit.OKOutput(d.Bytes())
}

// dump writes the value with the library's ordinary Marshal and default settings; the text is
// returned as it is.
func dump(v any) gokit.Result {
	b, err := oneyaml.Marshal(v)
	if err != nil {
		return fail(err)
	}
	return gokit.OKOutput(b)
}

func main() {
	a := gokit.Adapter{
		Hello: gokit.Hello{
			Name: "oneyaml-go", Version: version,
			Capabilities: []string{"events", "value", "error-category", "error-position",
				"patch:add", "patch:remove", "patch:replace", "patch:move", "patch:copy", "patch:test", "patch:merge", "dump"},
			Emulates: []string{},
			Info: map[string]string{
				"module": "oneyaml.dev/go " + version,
				"source": "a checkout named by KIT_ONEYAML_GO_SRC",
				"events": "Parse, then a walk of the Node tree; an error answer carries no events because Parse returns no partial document",
				"value":  "Decoder.Decode into any, every document",
				"errors": "SyntaxError (category, line, column), LimitError",
				"dump":   "Marshal on the native value, default settings",
				"patch":  "Document.ApplyPatch and ApplyMergePatch; the library is atomic and keeps the bytes",
			},
		},
		Events: events, Value: value, Patch: patch, Dump: dump,
		Binary: func(data []byte) (any, error) { return data, nil },
		BigInt: func(digits string) (any, error) {
			n, ok := new(big.Int).SetString(digits, 10)
			if !ok {
				return nil, errors.New("not a decimal integer")
			}
			return n, nil
		},
		AnyMaps: true,
	}
	a.Serve()
}
