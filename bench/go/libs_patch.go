package main

// Patch libraries. palantir's yamlpatch is the only formatting-aware RFC 6902 patcher in Go;
// evanphx's json-patch, reached through sigs.k8s.io/yaml, is the common patcher that works
// on JSON and so gives up the text. All three take text and return text, so the library's
// own parse and write are inside the timed work.

import (
	"encoding/json"

	jsonpatch "github.com/evanphx/json-patch/v5"
	"github.com/palantir/pkg/yamlpatch"
	"github.com/palantir/pkg/yamlpatch/goccyyamlpatcher"
	sigs "sigs.k8s.io/yaml"
)

func yamlpatchOps(ops []Op) (any, error) {
	var p yamlpatch.Patch
	for _, o := range ops {
		op := yamlpatch.Operation{Type: o.Op}
		var err error
		if op.Path, err = yamlpatch.ParsePath(o.Path); err != nil {
			return nil, err
		}
		if o.HasValue {
			op.Value = native(o.Value)
		}
		p = append(p, op)
	}
	return p, nil
}

func libPalantir() *yamlLib {
	return &yamlLib{
		id: "palantir-yamlpatch", pkg: "github.com/palantir/pkg/yamlpatch",
		settings: map[string]string{
			"api":     "yamlpatch.Apply (the yaml.v3 Node back end)",
			"backend": "gopkg.in/yaml.v3 " + moduleVersion("gopkg.in/yaml.v3"),
			"state":   "the text: the library parses and writes inside the timed work",
		},
		whyNoTyped: "a patcher only", whyNoGeneric: "a patcher only", whyNoDump: "a patcher only",
		whyNoDoc: "its API takes text and returns text; it has no document to hold",
		patch: &patchAPI{
			prepare: yamlpatchOps,
			apply: func(in []byte, p any) ([]byte, error) {
				return yamlpatch.Apply(in, p.(yamlpatch.Patch))
			},
		},
	}
}

func libPalantirGoccy() *yamlLib {
	return &yamlLib{
		id: "palantir-yamlpatch-goccy", pkg: "github.com/palantir/pkg/yamlpatch",
		settings: map[string]string{
			"api":     "goccyyamlpatcher.New().Apply (the goccy AST back end)",
			"backend": "github.com/goccy/go-yaml " + moduleVersion("github.com/goccy/go-yaml"),
			"state":   "the text: the library parses and writes inside the timed work",
		},
		whyNoTyped: "a patcher only", whyNoGeneric: "a patcher only", whyNoDump: "a patcher only",
		whyNoDoc: "its API takes text and returns text; it has no document to hold",
		patch: &patchAPI{
			prepare: yamlpatchOps,
			apply: func(in []byte, p any) ([]byte, error) {
				return goccyyamlpatcher.New().Apply(in, p.(yamlpatch.Patch))
			},
		},
	}
}

func libJSONPatch() *yamlLib {
	return &yamlLib{
		id: "json-patch", pkg: "github.com/evanphx/json-patch/v5",
		settings: map[string]string{
			"api":      "sigs.k8s.io/yaml.YAMLToJSON, jsonpatch.DecodePatch(...).Apply, sigs.k8s.io/yaml.JSONToYAML",
			"yaml":     "sigs.k8s.io/yaml " + moduleVersion("sigs.k8s.io/yaml"),
			"baseline": "not lossless: the patch is applied to JSON, so comments, order, quoting and layout are gone; judged on the value it holds (variant value-only)",
			"state":    "the text: the conversions are inside the timed work",
		},
		whyNoTyped: "a patcher only", whyNoGeneric: "a patcher only", whyNoDump: "a patcher only",
		whyNoDoc: "it works on JSON, which has no document of the YAML text",
		patch: &patchAPI{
			valueOnly: true,
			prepare: func(ops []Op) (any, error) {
				list := make([]map[string]any, 0, len(ops))
				for _, o := range ops {
					m := map[string]any{"op": o.Op, "path": o.Path}
					if o.HasValue {
						m["value"] = native(o.Value)
					}
					list = append(list, m)
				}
				raw, err := json.Marshal(list)
				if err != nil {
					return nil, err
				}
				p, err := jsonpatch.DecodePatch(raw)
				if err != nil {
					return nil, err
				}
				return p, nil
			},
			apply: func(in []byte, p any) ([]byte, error) {
				doc, err := sigs.YAMLToJSON(in)
				if err != nil {
					return nil, err
				}
				out, err := p.(jsonpatch.Patch).Apply(doc)
				if err != nil {
					return nil, err
				}
				return sigs.JSONToYAML(out)
			},
		},
	}
}
