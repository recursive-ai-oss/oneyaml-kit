package gokit

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
)

// Op is one RFC 6902 operation as the kit sends it.
type Op struct {
	Name     string
	Path     string
	HasPath  bool
	From     string
	HasFrom  bool
	Value    any
	HasValue bool
}

// ParsePatch reads the patch array of a case. Fields the adapter cannot interpret are
// left for the caller to report; this only splits the JSON.
func ParsePatch(raw json.RawMessage) ([]Op, error) {
	var items []map[string]json.RawMessage
	if err := json.Unmarshal(raw, &items); err != nil {
		return nil, errors.New("patch is not an array of objects")
	}
	ops := make([]Op, 0, len(items))
	for _, it := range items {
		var o Op
		if r, ok := it["op"]; ok {
			_ = json.Unmarshal(r, &o.Name)
		}
		if r, ok := it["path"]; ok {
			o.HasPath = json.Unmarshal(r, &o.Path) == nil
		}
		if r, ok := it["from"]; ok {
			o.HasFrom = json.Unmarshal(r, &o.From) == nil
		}
		if r, ok := it["value"]; ok {
			v, err := PatchValue(r)
			if err != nil {
				return nil, err
			}
			o.Value, o.HasValue = v, true
		}
		ops = append(ops, o)
	}
	return ops, nil
}

// Pair is one entry of an ordered mapping.
type Pair struct {
	Key   string
	Value any
}

// OMap is a JSON object with its keys in the order the patch wrote them, so a value added
// to a document keeps that order.
type OMap []Pair

// PatchValue reads a JSON value into Go values a YAML library can encode: integers stay
// integers, numbers with a point or exponent are floats, and objects are OMap.
func PatchValue(raw json.RawMessage) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	return readValue(dec)
}

func readValue(dec *json.Decoder) (any, error) {
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	switch t := tok.(type) {
	case json.Delim:
		if t == '[' {
			l := []any{}
			for dec.More() {
				v, err := readValue(dec)
				if err != nil {
					return nil, err
				}
				l = append(l, v)
			}
			_, err := dec.Token()
			return l, err
		}
		m := OMap{}
		for dec.More() {
			k, err := dec.Token()
			if err != nil {
				return nil, err
			}
			v, err := readValue(dec)
			if err != nil {
				return nil, err
			}
			m = append(m, Pair{k.(string), v})
		}
		_, err := dec.Token()
		return m, err
	case json.Number:
		s := string(t)
		if !strings.ContainsAny(s, ".eE") {
			if i, err := strconv.ParseInt(s, 10, 64); err == nil {
				return i, nil
			}
		}
		f, _ := strconv.ParseFloat(s, 64)
		return f, nil
	}
	return tok, nil
}

// Plain turns OMap values into Go maps, for libraries whose API takes a Go value: the
// order of keys is then whatever the library does with a map.
func Plain(v any) any {
	switch x := v.(type) {
	case OMap:
		m := make(map[string]any, len(x))
		for _, p := range x {
			m[p.Key] = Plain(p.Value)
		}
		return m
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = Plain(e)
		}
		return out
	}
	return v
}

// Build makes a node from a patch value, with the mapping and sequence constructors of the
// library's tree and its own encoding for scalars, so keys keep the patch's order.
func Build[N any](v any, scalar func(any) (N, error), mapping, sequence func([]N) N) (N, error) {
	switch x := v.(type) {
	case OMap:
		var kids []N
		for _, p := range x {
			k, err := scalar(p.Key)
			if err != nil {
				return k, err
			}
			c, err := Build(p.Value, scalar, mapping, sequence)
			if err != nil {
				return c, err
			}
			kids = append(kids, k, c)
		}
		return mapping(kids), nil
	case []any:
		var kids []N
		for _, e := range x {
			c, err := Build(e, scalar, mapping, sequence)
			if err != nil {
				return c, err
			}
			kids = append(kids, c)
		}
		return sequence(kids), nil
	}
	return scalar(v)
}

// Pointer splits an RFC 6901 JSON Pointer into its decoded tokens.
func Pointer(p string) ([]string, error) {
	if p == "" {
		return nil, nil
	}
	if p[0] != '/' {
		return nil, errors.New("a JSON Pointer must be empty or start with '/'")
	}
	parts := strings.Split(p[1:], "/")
	for i, t := range parts {
		if strings.Contains(t, "~") {
			t = strings.ReplaceAll(t, "~1", "/")
			t = strings.ReplaceAll(t, "~0", "~")
			parts[i] = t
		}
	}
	return parts, nil
}

// Kind is the shape of a node in a library's tree.
type Kind int

const (
	Scalar Kind = iota
	Mapping
	Sequence
	Alias
)

// Tree describes a library's node tree to the generic edit below. The library does the
// real editing work (encoding a value into a node, serialising the tree); the adapter
// only finds the node a JSON Pointer names.
type Tree[N comparable] struct {
	Kind    func(N) Kind
	Content func(N) []N
	// SetContent replaces the children of a mapping (keys and values alternating) or sequence.
	SetContent func(N, []N)
	// Text is the text of a scalar node.
	Text func(N) string
	// Target is the node an alias refers to.
	Target func(N) N
	// Set makes dst, in place, hold the given Go value, keeping its comments.
	Set func(dst N, value any) error
	// New builds a node holding the Go value.
	New func(value any) (N, error)
}

// Failure is a refused or failed patch step.
type Failure struct {
	Category string
	Message  string
	// Library is true when the library itself returned the error.
	Library bool
}

func fail(cat, format string, a ...any) *Failure {
	return &Failure{Category: cat, Message: fmt.Sprintf(format, a...)}
}

func libFail(err error) *Failure {
	return &Failure{Category: "other", Message: err.Error(), Library: true}
}

// Finish turns the outcome of Apply into a response. encode serialises the edited tree
// with the library. The names it attaches are the ones every edit adapter declares in
// its hello: "pointer" (the adapter, not the library, found the node), "error" (the
// adapter raised the error) and "unchanged" (the adapter reports the original bytes after
// a failure, because the library keeps no record of an abandoned edit). resolved says
// whether any pointer was resolved, so an empty patch is not marked.
func Finish(original []byte, f *Failure, unsupported string, resolved bool, encode func() ([]byte, error)) Result {
	if unsupported != "" {
		return Unsupported(unsupported)
	}
	if f != nil {
		r := Err(f.Category, f.Message, 0, 0).WithOutput(original)
		if f.Library {
			return r.WithEmulated("unchanged")
		}
		return r.WithEmulated("unchanged", "error")
	}
	out, err := encode()
	if err != nil {
		return Err("other", err.Error(), 0, 0).WithOutput(original).WithEmulated("unchanged")
	}
	r := OKOutput(out)
	if resolved {
		r = r.WithEmulated("pointer")
	}
	return r
}

func isIndex(t string) bool {
	if t == "" || (len(t) > 1 && t[0] == '0') {
		return false
	}
	for i := 0; i < len(t); i++ {
		if t[i] < '0' || t[i] > '9' {
			return false
		}
	}
	return true
}

// child finds the child a token names. For a mapping it returns the index of the value
// in the content slice; for a sequence the index of the item.
func (t Tree[N]) child(n N, tok string) (idx int, f *Failure) {
	if t.Kind(n) == Alias {
		n = t.Target(n)
	}
	c := t.Content(n)
	switch t.Kind(n) {
	case Mapping:
		// The first match in document order, as a user scanning the node's children would
		// take it. A second key with the same text is not looked for.
		for i := 0; i+1 < len(c); i += 2 {
			k := c[i]
			if t.Kind(k) == Scalar && t.Text(k) == tok {
				return i + 1, nil
			}
		}
		return 0, fail("patch-path", "no key %q", tok)
	case Sequence:
		if !isIndex(tok) {
			return 0, fail("patch-path", "%q is not a sequence index", tok)
		}
		i, err := strconv.Atoi(tok)
		if err != nil || i >= len(c) {
			return 0, fail("patch-path", "index %s is out of range", tok)
		}
		return i, nil
	}
	return 0, fail("patch-path", "cannot step into a scalar with %q", tok)
}

// resolve finds the node a token path names. Aliases are followed to the node they refer
// to, as a user of the library's tree would; whether writing there is acceptable is not
// decided here.
func (t Tree[N]) resolve(root N, toks []string) (N, *Failure) {
	cur := root
	for _, tok := range toks {
		idx, f := t.child(cur, tok)
		if f != nil {
			return cur, f
		}
		if t.Kind(cur) == Alias {
			cur = t.Target(cur)
		}
		cur = t.Content(cur)[idx]
	}
	return cur, nil
}

// Apply runs the operations that map onto a node edit: add, replace and remove. It
// returns nil on success and the first failure otherwise; the tree is then not to be used.
// An unsupported operation or the whole document as a target returns unsupported=true.
func (t Tree[N]) Apply(root N, ops []Op) (f *Failure, unsupported string) {
	for _, op := range ops {
		switch op.Name {
		case "add", "replace", "remove", "move", "copy", "test":
		default:
			return fail("patch-invalid", "unknown operation %q", op.Name), ""
		}
		if !op.HasPath {
			return fail("patch-invalid", "operation without a path"), ""
		}
		toks, err := Pointer(op.Path)
		if err != nil {
			return fail("patch-invalid", "%v", err), ""
		}
		if len(toks) == 0 {
			return nil, "a patch of the whole document"
		}
		switch op.Name {
		case "move", "copy", "test":
			return nil, "patch operation " + op.Name
		case "replace", "add":
			if !op.HasValue {
				return fail("patch-invalid", "%s without a value", op.Name), ""
			}
		}
		parentPath, last := toks[:len(toks)-1], toks[len(toks)-1]
		parent, f := t.resolve(root, parentPath)
		if f != nil {
			return f, ""
		}
		if t.Kind(parent) == Alias {
			parent = t.Target(parent)
		}
		switch op.Name {
		case "replace":
			i, f := t.child(parent, last)
			if f != nil {
				return f, ""
			}
			if err := t.Set(t.Content(parent)[i], op.Value); err != nil {
				return libFail(err), ""
			}
		case "remove":
			i, f := t.child(parent, last)
			if f != nil {
				return f, ""
			}
			c := t.Content(parent)
			if t.Kind(parent) == Mapping {
				t.SetContent(parent, append(append([]N{}, c[:i-1]...), c[i+1:]...))
			} else {
				t.SetContent(parent, append(append([]N{}, c[:i]...), c[i+1:]...))
			}
		case "add":
			v, err := t.New(op.Value)
			if err != nil {
				return libFail(err), ""
			}
			c := t.Content(parent)
			switch t.Kind(parent) {
			case Mapping:
				i, f := t.child(parent, last)
				if f == nil {
					if err := t.Set(c[i], op.Value); err != nil {
						return libFail(err), ""
					}
					continue
				}
				if f.Category != "patch-path" {
					return f, ""
				}
				k, err := t.New(last)
				if err != nil {
					return libFail(err), ""
				}
				t.SetContent(parent, append(append([]N{}, c...), k, v))
			case Sequence:
				at := len(c)
				if last != "-" {
					if !isIndex(last) {
						return fail("patch-path", "%q is not a sequence index", last), ""
					}
					at, err = strconv.Atoi(last)
					if err != nil || at > len(c) {
						return fail("patch-path", "index %s is out of range", last), ""
					}
				}
				n := append(append(append([]N{}, c[:at]...), v), c[at:]...)
				t.SetContent(parent, n)
			default:
				return fail("patch-path", "cannot add into a scalar"), ""
			}
		}
	}
	return nil, ""
}
