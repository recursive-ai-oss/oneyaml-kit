package main

// The reference parsers read a dumped document back. Two parsers that share no code must
// agree on what the text means: go.yaml.in/yaml/v3 (a port of libyaml) and goccy/go-yaml (a
// parser written from scratch). When they disagree, or only one accepts the text, the output
// cannot be judged and the cell says so instead of blaming the library under test. Both are
// also libraries that are measured; here they only read.

import (
	"bytes"
	"errors"
	"fmt"
	"io"

	goccy "github.com/goccy/go-yaml"
	yamlv3 "go.yaml.in/yaml/v3"
)

func readAll(next func(any) error) ([]any, error) {
	var docs []any
	for {
		var v any
		err := next(&v)
		if errors.Is(err, io.EOF) {
			return docs, nil
		}
		if err != nil {
			return nil, err
		}
		docs = append(docs, v)
	}
}

func readCommon(next func(any) error) (any, error) {
	docs, err := readAll(next)
	if err != nil {
		return nil, err
	}
	out := make([]any, len(docs))
	for i, d := range docs {
		c, err := fromAny(d)
		if err != nil {
			return nil, err
		}
		out[i] = c
	}
	return docsValue(out), nil
}

func refereeV3(b []byte) (any, error) {
	return readCommon(yamlv3.NewDecoder(bytes.NewReader(b)).Decode)
}

func refereeGoccy(b []byte) (any, error) {
	return readCommon(goccy.NewDecoder(bytes.NewReader(b)).Decode)
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
