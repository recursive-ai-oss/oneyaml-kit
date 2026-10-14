package main

// Typed models for the typed-load task: Go structs that hold every field of the workload, so
// the decoded value can be compared with the whole expected value. Both the `yaml` and the
// `json` tags are set, because the libraries read different ones (sigs.k8s.io/yaml reads
// `json`). Where the other languages' models use a union (a string or a mapping, a boolean
// or a string), Go has no sum type and the field is `any`, which is how a Go program models
// it; the library then decodes it to its own generic form.

type Record struct {
	ID      uint64   `yaml:"id" json:"id"`
	Name    string   `yaml:"name" json:"name"`
	Email   string   `yaml:"email" json:"email"`
	Active  bool     `yaml:"active" json:"active"`
	Score   float64  `yaml:"score" json:"score"`
	Tags    []string `yaml:"tags" json:"tags"`
	Address Address  `yaml:"address" json:"address"`
	Note    *string  `yaml:"note" json:"note"`
}

type Address struct {
	City string `yaml:"city" json:"city"`
	Zip  string `yaml:"zip" json:"zip"`
	Geo  *Geo   `yaml:"geo" json:"geo" kit:"skip-none"`
}

type Geo struct {
	Lat float64 `yaml:"lat" json:"lat"`
	Lng float64 `yaml:"lng" json:"lng"`
}

type Workflow struct {
	Name string         `yaml:"name" json:"name"`
	On   On             `yaml:"on" json:"on"`
	Jobs map[string]Job `yaml:"jobs" json:"jobs"`
}

type On struct {
	PullRequest *string `yaml:"pull_request" json:"pull_request"`
	Push        Push    `yaml:"push" json:"push"`
}

type Push struct {
	Branches []string `yaml:"branches" json:"branches"`
}

type Job struct {
	RunsOn    string             `yaml:"runs-on" json:"runs-on"`
	Strategy  *Strategy          `yaml:"strategy" json:"strategy" kit:"skip-none"`
	Container any                `yaml:"container" json:"container" kit:"skip-none"`
	Services  map[string]Service `yaml:"services" json:"services" kit:"skip-none"`
	Env       map[string]string  `yaml:"env" json:"env" kit:"skip-none"`
	Steps     []Step             `yaml:"steps" json:"steps"`
}

type Strategy struct {
	Matrix Matrix `yaml:"matrix" json:"matrix"`
}

type Matrix struct {
	RunsOn []string `yaml:"runs-on" json:"runs-on"`
}

type Service struct {
	Image string   `yaml:"image" json:"image"`
	Ports []string `yaml:"ports" json:"ports"`
}

type Step struct {
	ID        *string        `yaml:"id" json:"id" kit:"skip-none"`
	Name      *string        `yaml:"name" json:"name" kit:"skip-none"`
	Condition *string        `yaml:"if" json:"if" kit:"skip-none"`
	Uses      *string        `yaml:"uses" json:"uses" kit:"skip-none"`
	Run       *string        `yaml:"run" json:"run" kit:"skip-none"`
	Shell     *string        `yaml:"shell" json:"shell" kit:"skip-none"`
	With      map[string]any `yaml:"with" json:"with" kit:"skip-none"`
}

// typedParse decodes the input into the model the workload names, with a library's entry
// point for typed decoding, and returns the result in the common form.
func typedRunner(parse func(in []byte, out any) error, model string, in []byte) (Runner, string) {
	switch model {
	case "gha":
		return typedOf[Workflow](parse, in), ""
	case "records":
		return typedOf[[]Record](parse, in), ""
	}
	return nil, "the workload names no typed model"
}
