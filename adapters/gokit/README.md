# Go adapters

Each Go library under test has its own directory in `adapters/`, its own Go module with
the library pinned to an exact version (`go.mod`, `go.sum`), and a `main.go` that
connects it to the kit through the [adapter protocol](../../docs/adapter-protocol.md).
This directory holds what they share. It is not an adapter itself.

| Adapter | Library | Offers |
|---|---|---|
| `go-yaml-v2` | `gopkg.in/yaml.v2` | values, accept or reject |
| `go-yaml-v3` | `go.yaml.in/yaml/v3` (the module `gopkg.in/yaml.v3` moved to) | values, accept or reject, `yaml.Node` edits: add, remove, replace |
| `go-yaml-v4` | `go.yaml.in/yaml/v4` (release candidate) | the same, with error stage and position |
| `sigs-k8s-yaml` | `sigs.k8s.io/yaml` | values (first document), accept or reject |
| `goccy-go-yaml` | `github.com/goccy/go-yaml` | values, accept or reject with positions, AST edits: replace |
| `palantir-yamlpatch-v3` | `github.com/palantir/pkg/yamlpatch`, default back end | RFC 6902 patches |
| `palantir-yamlpatch-goccy` | the same library, goccy back end | RFC 6902 patches |

## Running

```sh
bin/kit fast --adapter go-yaml-v3          # build, run the whole fast set, write out/
KIT_GO_BIN=/some/dir bin/kit fast          # keep the built binaries out of the checkout
```

The manifest's build step compiles the adapter with `build.sh`; `run.sh` replaces itself
with the binary, so the process the kit measures for memory is the one running the
library. A Go toolchain (see `go.mod` for the version) and the modules in the Go module
cache are all that is needed. Binaries go to `KIT_GO_BIN`, or to the ignored `.bin/`
directory here.

## What an adapter declares, and why

- **No library here has an event stream.** The parsers declare `outcome:events` and not
  `events`. For the events operation an adapter loads every document into generic values:
  input the library rejects is answered with an error, and input it takes is answered
  `accepted`. So the 94 invalid yaml-test-suite inputs are judged on what the library does
  (one it accepts is a failure), and a valid input is a failure only if the library rejects
  it. No event is compared.
- **Values** are what the library returns when decoding every document into `any`
  (`sigs-k8s-yaml`: the JSON text the library produces for the first document). The
  adapter converts Go types to the kit's value notation and nothing else; a Go type it does
  not know is declined, never guessed. A timestamp the library resolves is written as its
  text, and bytes as the string they spell, so these differ from the expected plain text
  and the case fails, as it should.
- **Errors.** `go-yaml-v4` and `goccy-go-yaml` report a position (and a stage or error
  type) in their error values and declare `error-position` and `error-category`; the other
  libraries return plain strings, so they declare neither, and strict checks that need them are
  unsupported. The adapters do not read positions out of message text.
- **Edits.** The Node and AST libraries have no pointer or patch language. The adapter
  walks the tree to the node a JSON Pointer names, the way a user of the tree would:
  aliases are followed to the anchored node, and the first key that matches in document order
  is taken. It refuses neither a write through an alias nor an ambiguous key, so those cases
  show what the library itself does. The adapter then edits as a user would. `go-yaml-v3`
  and `go-yaml-v4` set the existing scalar node's text and tag from the library's own encoding
  of the new value (the node keeps its style and comments, and the library's encoder chooses
  the block header or quoting the text needs), and replace a collection with a freshly built
  node, in the key order the patch wrote. `goccy-go-yaml` writes the library's YAML Path and
  lets its replace do the work.
  What the adapter itself supplies is declared as emulated behaviour: `pointer` (the adapter
  found the node), `error` (the adapter raised the error: a missing path, an unknown
  operation) and `unchanged` (the adapter reports the original bytes after a failed patch).
  Passes that depend on them are marked in the *Emulated* column. `palantir-yamlpatch-*` is a
  real patcher: only `unchanged` is the adapter's, because the library returns no bytes when
  it fails. Its values reach the library as Go maps, as a Go caller would write them.
- **Panics end the process.** A panic inside a library is never turned into an error answer,
  which would let a crash pass as a rejection. The kit restarts the adapter and counts the
  case as failed.

## Known deviations

Each adapter's `deviations.json` lists every case it does not pass, with the comparison's
own account of the difference as the reason. The lists are generated:

```sh
adapters/gokit/record_deviations.py go-yaml-v3 goccy-go-yaml
```

The script runs the fast set against an empty list and writes what it finds. Read the diff
of a regenerated list before accepting it: the run fails on any failure that is not
listed and on any entry that no longer holds, so the list is only as good as the look
someone gave it.

## Tests

`go test ./...` here covers the value notation, the pointer handling and the response
encoding. `tests/test_adapters_go.py` pins the declared capabilities, the library
versions, the yaml-test-suite numbers and the reference edit cases for every Go adapter.
