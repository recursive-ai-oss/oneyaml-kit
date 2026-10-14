module example.invalid/oneyaml-kit/adapters/sigs-k8s-yaml

go 1.26

require (
	example.invalid/oneyaml-kit/gokit v0.0.0
	sigs.k8s.io/yaml v1.6.0
)

require go.yaml.in/yaml/v2 v2.4.2 // indirect

replace example.invalid/oneyaml-kit/gokit => ../gokit
