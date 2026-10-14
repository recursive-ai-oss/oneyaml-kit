package main

// Editing a node tree the way a user of a tree API does: walk to the node the path names by
// scanning the children, then change it in place. The same walk serves every library whose
// document is a tree of nodes; the library supplies what a node is and how a value is put
// into a scalar.

import (
	"fmt"
	"strconv"
)

type nodeKind int

const (
	kScalar nodeKind = iota
	kMapping
	kSequence
	kAlias
	kOther
)

type nodeOps[N comparable] struct {
	kind       func(N) nodeKind
	content    func(N) []N
	setContent func(N, []N)
	text       func(N) string
	target     func(N) N
	// setScalar puts a Go value into an existing scalar node, as the library's own API does.
	setScalar func(N, any) error
}

func (o nodeOps[N]) find(n N, seg Seg) (idx int, err error) {
	if o.kind(n) == kAlias {
		n = o.target(n)
	}
	c := o.content(n)
	switch o.kind(n) {
	case kMapping:
		if seg.IsIdx {
			return 0, fmt.Errorf("an index into a mapping")
		}
		for i := 0; i+1 < len(c); i += 2 {
			if o.kind(c[i]) == kScalar && o.text(c[i]) == seg.Key {
				return i + 1, nil
			}
		}
		return 0, fmt.Errorf("no key %q", seg.Key)
	case kSequence:
		if !seg.IsIdx || seg.Index >= len(c) {
			return 0, fmt.Errorf("no element %s", seg.Key+strconv.Itoa(seg.Index))
		}
		return seg.Index, nil
	}
	return 0, fmt.Errorf("cannot step into a scalar")
}

// edit applies the steps (replace and remove) to the document root.
func (o nodeOps[N]) edit(root N, steps []EditStep) error {
	for _, st := range steps {
		if len(st.Segs) == 0 {
			return fmt.Errorf("an edit of the whole document")
		}
		cur := root
		for _, s := range st.Segs[:len(st.Segs)-1] {
			i, err := o.find(cur, s)
			if err != nil {
				return fmt.Errorf("%s: %w", st.Dotted, err)
			}
			if o.kind(cur) == kAlias {
				cur = o.target(cur)
			}
			cur = o.content(cur)[i]
		}
		if o.kind(cur) == kAlias {
			cur = o.target(cur)
		}
		last := st.Segs[len(st.Segs)-1]
		i, err := o.find(cur, last)
		if err != nil {
			return fmt.Errorf("%s: %w", st.Dotted, err)
		}
		c := o.content(cur)
		switch st.Op {
		case "replace":
			if err := o.setScalar(c[i], st.Value); err != nil {
				return fmt.Errorf("%s: %w", st.Dotted, err)
			}
		case "remove":
			lo := i
			if o.kind(cur) == kMapping {
				lo = i - 1
			}
			o.setContent(cur, append(c[:lo], c[i+1:]...))
		default:
			return fmt.Errorf("unsupported edit %s", st.Op)
		}
	}
	return nil
}
