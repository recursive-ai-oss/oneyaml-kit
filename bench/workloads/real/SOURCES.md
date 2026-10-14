# Real-world workloads

Each directory holds one file vendored byte for byte as `input.yaml`, with its origin,
revision and licence in `workload.json`. The files are used only as benchmark input and
are redistributed under the licences below.

| Workload | Repository | Licence |
|---|---|---|
| `gha-workflow` | actions/checkout | MIT |
| `compose-wordpress` | docker/awesome-compose | CC0-1.0 |
| `helm-ingress-nginx`, `k8s-ingress-nginx-multi` | kubernetes/ingress-nginx | Apache-2.0 |
| `prometheus-config` | prometheus/prometheus | Apache-2.0 |
| `openapi-petstore`, `openapi-api-with-examples` | OAI/learn.openapis.org | CC-BY-4.0 (attribution: OpenAPI Initiative) |

The exact revision and path of each file are in its `workload.json`.

`expected.json` beside each input is the value of the document. It is not taken from any
one library: it was frozen from two independent YAML 1.2 parsers that agree on every
value, and checked against PyYAML (YAML 1.1) so that no scalar whose meaning differs
between 1.1 and 1.2 is left unexamined. `bin/bench freeze` reproduces the check.
A digest of each input and expectation is pinned in `bench/tests/pins.json`.
