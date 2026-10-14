// Kit adapter for gopkg.in/yaml.v2.
//
// The library has no event stream and no node tree, so it offers values and the accept or
// reject decision only, and no edits. Valid input is declined for the events operation.
package main

import (
	"bytes"
	"errors"
	"io"

	"example.invalid/oneyaml-kit/gokit"
	yaml "gopkg.in/yaml.v2"
)

const version = "v2.4.0"

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

func main() {
	a := gokit.Adapter{
		Hello: gokit.Hello{
			Name: "go-yaml-v2", Version: version,
			Capabilities: []string{"events", "value"},
			Info: map[string]string{
				"module": "gopkg.in/yaml.v2 " + version,
				"events": "no event stream: valid input is declined; accept or reject is a generic load of every document",
				"value":  "Decoder.Decode into any, every document",
				"errors": "plain error strings: no category and no position are reported",
			},
		},
		Events: events, Value: value,
	}
	a.Serve()
}
