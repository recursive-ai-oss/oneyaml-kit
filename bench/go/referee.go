package main

// The reference reading of a dumped document. Two parsers that share no code, go.yaml.in/yaml/v3
// (a port of libyaml) and goccy/go-yaml (a parser written from scratch), give the structure of
// the text: the nodes with their style, anchors, aliases and explicit tags. Neither is asked
// what a plain scalar means. The harness resolves it itself, by the YAML 1.2 core schema, and
// never applies a merge key, so the text is judged the way the specification reads it and not by
// either library's idea of a type. When the two readings differ, or only one parser accepts the
// text, the output cannot be judged and the cell says so instead of blaming the library under
// test. Both parsers are also libraries that are measured; here they only read.

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"math"
	"math/big"
	"regexp"
	"strconv"
	"strings"

	"github.com/goccy/go-yaml/ast"
	"github.com/goccy/go-yaml/parser"
	"github.com/goccy/go-yaml/token"
	yamlv3 "go.yaml.in/yaml/v3"
)

type rkind int

const (
	rScalar rkind = iota
	rMap
	rSeq
	rAlias
)

// rnode is one node as both parsers are reduced to it.
type rnode struct {
	kind   rkind
	anchor string
	tag    string // an explicit tag, as written, or ""
	value  string // a scalar's text, or an alias's name
	plain  bool   // a scalar in plain style
	kids   []*rnode
}

var (
	intRe   = regexp.MustCompile(`^[-+]?[0-9]+$`)
	octRe   = regexp.MustCompile(`^0o[0-7]+$`)
	hexRe   = regexp.MustCompile(`^0x[0-9a-fA-F]+$`)
	floatRe = regexp.MustCompile(`^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?$`)
	infRe   = regexp.MustCompile(`^[-+]?\.(inf|Inf|INF)$`)
	nanRe   = regexp.MustCompile(`^\.(nan|NaN|NAN)$`)
)

// resolveCore resolves a plain scalar by the YAML 1.2 core schema (section 10.3.2). `1_000` and
// `0b101` are text, `017` is the decimal 17, and `yes` is text.
func resolveCore(s string) any {
	switch s {
	case "", "~", "null", "Null", "NULL":
		return nil
	case "true", "True", "TRUE":
		return true
	case "false", "False", "FALSE":
		return false
	}
	switch {
	case intRe.MatchString(s):
		n, _ := new(big.Int).SetString(s, 10)
		return Int(n.String())
	case octRe.MatchString(s):
		n, _ := new(big.Int).SetString(s[2:], 8)
		return Int(n.String())
	case hexRe.MatchString(s):
		n, _ := new(big.Int).SetString(s[2:], 16)
		return Int(n.String())
	case floatRe.MatchString(s):
		f, err := strconv.ParseFloat(s, 64)
		if err != nil && !errors.Is(err, strconv.ErrRange) {
			return s
		}
		return f
	case infRe.MatchString(s):
		if s[0] == '-' {
			return math.Inf(-1)
		}
		return math.Inf(1)
	case nanRe.MatchString(s):
		return math.NaN()
	}
	return s
}

// build turns the nodes of one document into the common form. Anchors are kept by name; an
// alias is the value its anchor had. Mappings are plain mappings: `<<` is an ordinary key.
type builder struct{ anchors map[string]any }

func (b *builder) value(n *rnode) (any, error) {
	if n == nil {
		return nil, nil
	}
	var v any
	switch n.kind {
	case rAlias:
		t, ok := b.anchors[n.value]
		if !ok {
			return nil, fmt.Errorf("unknown alias *%s", n.value)
		}
		return t, nil
	case rScalar:
		switch {
		case n.tag == "!" || n.tag == "!!str" || n.tag == "tag:yaml.org,2002:str":
			v = n.value
		case n.tag != "" && n.tag != "!!null" && n.tag != "!!bool" && n.tag != "!!int" && n.tag != "!!float":
			return nil, fmt.Errorf("explicit tag %s", n.tag)
		case !n.plain && n.tag == "":
			v = n.value
		default:
			v = resolveCore(n.value)
		}
	case rSeq:
		l := make([]any, 0, len(n.kids))
		for _, k := range n.kids {
			e, err := b.value(k)
			if err != nil {
				return nil, err
			}
			l = append(l, e)
		}
		v = l
	case rMap:
		m := make(map[string]any, len(n.kids)/2)
		for i := 0; i+1 < len(n.kids); i += 2 {
			k, err := b.value(n.kids[i])
			if err != nil {
				return nil, err
			}
			ks, err := keyString(k)
			if err != nil {
				return nil, err
			}
			e, err := b.value(n.kids[i+1])
			if err != nil {
				return nil, err
			}
			m[ks] = e
		}
		v = m
	}
	if n.anchor != "" {
		b.anchors[n.anchor] = v
	}
	return v, nil
}

func buildDocs(docs []*rnode) (any, error) {
	out := make([]any, 0, len(docs))
	for _, d := range docs {
		b := &builder{anchors: map[string]any{}}
		v, err := b.value(d)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return docsValue(out), nil
}

// ---- go.yaml.in/yaml/v3 ---------------------------------------------------------------------

func fromV3(n *yamlv3.Node) *rnode {
	switch n.Kind {
	case yamlv3.DocumentNode:
		if len(n.Content) == 0 {
			return nil
		}
		return fromV3(n.Content[0])
	case yamlv3.AliasNode:
		return &rnode{kind: rAlias, value: n.Value}
	case yamlv3.ScalarNode:
		r := &rnode{kind: rScalar, anchor: n.Anchor, value: n.Value,
			plain: n.Style&(yamlv3.DoubleQuotedStyle|yamlv3.SingleQuotedStyle|yamlv3.LiteralStyle|yamlv3.FoldedStyle) == 0}
		// The library gives every plain scalar a resolved tag of its own; only a tag the text
		// carried is an explicit one.
		if n.Style&yamlv3.TaggedStyle != 0 {
			r.tag = n.Tag
		}
		return r
	}
	r := &rnode{kind: rMap, anchor: n.Anchor}
	if n.Kind == yamlv3.SequenceNode {
		r.kind = rSeq
	}
	for _, c := range n.Content {
		r.kids = append(r.kids, fromV3(c))
	}
	return r
}

func readV3(b []byte) ([]*rnode, error) {
	dec := yamlv3.NewDecoder(bytes.NewReader(b))
	var docs []*rnode
	for {
		var n yamlv3.Node
		err := dec.Decode(&n)
		if errors.Is(err, io.EOF) {
			return docs, nil
		}
		if err != nil {
			return nil, err
		}
		docs = append(docs, fromV3(&n))
	}
}

// ---- goccy/go-yaml --------------------------------------------------------------------------

func fromGoccy(n ast.Node) (*rnode, error) {
	switch x := n.(type) {
	case nil:
		return nil, nil
	case *ast.DocumentNode:
		return fromGoccy(x.Body)
	case *ast.AnchorNode:
		r, err := fromGoccy(x.Value)
		if r == nil || err != nil {
			return &rnode{kind: rScalar, plain: true, anchor: x.Name.String()}, err
		}
		r.anchor = x.Name.String()
		return r, nil
	case *ast.AliasNode:
		return &rnode{kind: rAlias, value: x.Value.String()}, nil
	case *ast.TagNode:
		r, err := fromGoccy(x.Value)
		if r == nil || err != nil {
			return &rnode{kind: rScalar, plain: true, tag: x.Start.Value}, err
		}
		r.tag = x.Start.Value
		return r, nil
	case *ast.MappingKeyNode:
		return fromGoccy(x.Value)
	case *ast.MappingValueNode:
		return mapOf([]*ast.MappingValueNode{x})
	case *ast.MappingNode:
		return mapOf(x.Values)
	case *ast.SequenceNode:
		r := &rnode{kind: rSeq}
		for _, v := range x.Values {
			k, err := fromGoccy(v)
			if err != nil {
				return nil, err
			}
			r.kids = append(r.kids, k)
		}
		return r, nil
	case *ast.LiteralNode:
		return &rnode{kind: rScalar, value: x.Value.Value}, nil
	case *ast.StringNode:
		switch x.Token.Type {
		case token.SingleQuoteType, token.DoubleQuoteType:
			return &rnode{kind: rScalar, value: x.Value}, nil
		}
		return &rnode{kind: rScalar, plain: true, value: plainText(x.Token, x.Value)}, nil
	case *ast.NullNode, *ast.IntegerNode, *ast.FloatNode, *ast.BoolNode, *ast.InfinityNode, *ast.NanNode, *ast.MergeKeyNode:
		// The text as written. The library has decided what it means; the harness does not ask.
		tk := n.GetToken()
		return &rnode{kind: rScalar, plain: true, value: plainText(tk, tk.Value)}, nil
	}
	return nil, fmt.Errorf("node of kind %T", n)
}

// plainText is the text of a plain scalar as it was written. The library's lexer drops a tab
// inside a plain scalar from the value it reports, but keeps the source text; a scalar on one
// line is read from that, and a scalar that continues on further lines from the value.
func plainText(tk *token.Token, value string) string {
	if tk == nil {
		return value
	}
	o := strings.TrimSpace(tk.Origin)
	if o != "" && !strings.ContainsAny(o, "\r\n") {
		return o
	}
	return value
}

func mapOf(vals []*ast.MappingValueNode) (*rnode, error) {
	r := &rnode{kind: rMap}
	for _, mv := range vals {
		k, err := fromGoccy(mv.Key)
		if err != nil {
			return nil, err
		}
		v, err := fromGoccy(mv.Value)
		if err != nil {
			return nil, err
		}
		if v == nil {
			v = &rnode{kind: rScalar, plain: true}
		}
		r.kids = append(r.kids, k, v)
	}
	return r, nil
}

func readGoccy(b []byte) ([]*rnode, error) {
	f, err := parser.ParseBytes(b, 0)
	if err != nil {
		return nil, err
	}
	var docs []*rnode
	for _, d := range f.Docs {
		r, err := fromGoccy(d)
		if err != nil {
			return nil, err
		}
		docs = append(docs, r)
	}
	return docs, nil
}

// ---- the two readings -----------------------------------------------------------------------

func refereeV3(b []byte) (any, error) {
	docs, err := readV3(b)
	if err != nil {
		return nil, err
	}
	return buildDocs(docs)
}

func refereeGoccy(b []byte) (any, error) {
	docs, err := readGoccy(b)
	if err != nil {
		return nil, err
	}
	return buildDocs(docs)
}

func twoReferees(b []byte) (any, *refereeError) {
	a, ea := refereeV3(b)
	c, ec := refereeGoccy(b)
	switch {
	case ea == nil && ec == nil:
		if d := firstDiff(a, c, ""); d != "" {
			return nil, &refereeError{msg: "the two reference parsers disagree " + d}
		}
		return a, nil
	case ea != nil && ec != nil:
		return nil, &refereeError{rejected: true, msg: fmt.Sprintf("yaml.v3: %v; goccy: %v", ea, ec)}
	case ea != nil:
		return nil, &refereeError{msg: fmt.Sprintf("yaml.v3 rejects it, goccy reads it: %v", ea)}
	}
	return nil, &refereeError{msg: fmt.Sprintf("goccy rejects it, yaml.v3 reads it: %v", ec)}
}
