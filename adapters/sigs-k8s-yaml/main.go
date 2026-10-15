// Kit adapter for sigs.k8s.io/yaml.
//
// The library converts YAML to JSON and offers no events, no node tree and no edits. The
// adapter reads the JSON text the library returns, so integers and floats keep the form
// the library gave them. The library reads one document, so a stream is answered with the
// first document only; that is the library's behaviour, not a choice of the adapter.
package main

import (
	"example.invalid/oneyaml-kit/gokit"
	"sigs.k8s.io/yaml"
)

const version = "v1.6.0"

func events(in []byte) gokit.Result {
	if _, err := yaml.YAMLToJSON(in); err != nil {
		return gokit.Err("other", err.Error(), 0, 0)
	}
	return gokit.Accepted()
}

func value(in []byte) gokit.Result {
	j, err := yaml.YAMLToJSON(in)
	if err != nil {
		return gokit.Err("other", err.Error(), 0, 0)
	}
	v, err := gokit.FromJSON(j)
	if err != nil {
		return gokit.Unsupported(err.Error())
	}
	out, err := gokit.Documents([]any{v})
	if err != nil {
		return gokit.Unsupported(err.Error())
	}
	return gokit.OKValue(out)
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
			Name: "sigs-k8s-yaml", Version: version,
			Capabilities: []string{"outcome:events", "value", "dump"},
			Info: map[string]string{
				"dump":   "yaml.Marshal (through JSON), default settings",
				"module": "sigs.k8s.io/yaml " + version,
				"events": "no event stream: accept or reject is YAMLToJSON of the first document",
				"value":  "YAMLToJSON, first document only; the JSON text is read back with integers and floats kept apart",
				"errors": "plain error strings: no category and no position are reported",
			},
		},
		Events: events, Value: value, Dump: dump,
		// A byte slice goes through JSON, which writes it as untagged base64 text: the case
		// judges that text, and it does not read back as the bytes.
		Binary: func(data []byte) (any, error) { return data, nil },
	}
	a.Serve()
}
