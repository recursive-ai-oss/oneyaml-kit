module example.invalid/oneyaml-kit/adapters/palantir-yamlpatch-v3

go 1.26

require (
	example.invalid/oneyaml-kit/gokit v0.0.0
	github.com/palantir/pkg/yamlpatch v1.5.0
)

require (
	github.com/palantir/pkg v1.1.0 // indirect
	github.com/pkg/errors v0.9.1 // indirect
	gopkg.in/yaml.v3 v3.0.1 // indirect
)

replace example.invalid/oneyaml-kit/gokit => ../gokit
