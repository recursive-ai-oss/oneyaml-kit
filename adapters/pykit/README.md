# Python adapters

Each Python library under test has its own directory in `adapters/` with a `main.py` that connects it
to the kit through the [adapter protocol](../../docs/adapter-protocol.md). This directory holds what
they share. It is not an adapter itself.

| Adapter | Library | Offers |
|---|---|---|
| `pyyaml-c` | PyYAML 6.0.3, `CSafeLoader` and `CSafeDumper` (libyaml) | events, values, dump, error category and position |
| `pyyaml-pure` | PyYAML 6.0.3, `SafeLoader` and `SafeDumper` | the same |
| `ruamel-safe` | ruamel.yaml 0.19.1, `typ='safe', pure=True` | the same |
| `ruamel-rt` | ruamel.yaml 0.19.1, round-trip | the same, and RFC 6902 / 7396 patches through jsonpatch 1.36 and json-merge-patch 0.3.0 |
| `yamlrocks` | yamlrocks 0.7.1 | accept or reject, values, dump, error category and position, `OPT_ROUND_TRIP` edits: add (mappings), remove, replace |
| `yaml-rs` | yaml-rs 0.1.6 | accept or reject, values, dump |
| `py-yaml12` | py-yaml12 0.2.0 | the same |
| `ryaml` | ryaml 0.5.1 | the same |
| `oneyaml-python`, `oneyaml-python-pure` | the Python library, from a local checkout (optional, see [its README](../oneyaml-python/README.md)) | nothing yet |

## Running

```sh
bin/kit fast --adapter pyyaml-c
KIT_PY_ENV=/some/dir bin/kit fast        # keep the virtual environments out of the checkout
```

The manifest's build step (`build.sh`) creates one virtual environment for all the library adapters on
CPython 3.13 (uv fetches it if needed) and installs `libs.lock`: exact versions, every file hashed,
binary wheels only, so nothing is compiled. The build fails if the PyYAML wheel has no libyaml. The
environment lives in `KIT_PY_ENV`, else beside the kit's build output directory (`pyenv` next to
`CARGO_TARGET_DIR`), else under the user cache. `run.sh` replaces itself with the interpreter, so the
process the kit measures for memory is the one running the library.

`libs.in` lists the pins; to move one, edit it and regenerate the lock:

```sh
uv pip compile --universal --generate-hashes libs.in -o libs.lock
```

`pykit.py` is the shared helper (standard library only): the JSON Lines loop, base64, the kit's value
notation written with an explicit stack (a value a hundred thousand levels deep needs no recursion),
text decoding for libraries that read `str`, and event lines. `pyyaml_common.py`, `ruamel_common.py` and
`textlibs.py` hold what the adapters of one family share.

## What an adapter declares, and why

- **Events** only where the library has an event stream: PyYAML and ruamel.yaml. The adapter writes
  the kit's notation from the library's own events (style, flow marker, anchor, resolved tag, explicit
  markers) and returns the events read before an error. The others declare `outcome:events`: input the
  library rejects is an error, input it takes is `accepted`.
- **Values** are what the library returns, converted to the kit's notation and nothing else. A timestamp
  the library resolves is written as its text and bytes as the string they spell, so these differ from
  the expected plain text and the case fails, as it should. Tagged nodes a library keeps (ruamel's
  `TaggedScalar`, py-yaml12's `Yaml`) are written as `$tagged`. A graph that expands past two million
  nodes (a library that loaded an alias bomb without copying) is answered `accepted`; a graph that
  refers to itself is an error the adapter raises (`error`), because the notation has no cycles.
- **yaml-rs** returns one document as itself and several as a list, so a list is ambiguous. The adapter
  parses a second time with an empty document appended: several documents grow by one, a single
  sequence becomes exactly two.
- **Text libraries** (yaml-rs, py-yaml12, ryaml) read `str`. The adapter detects the encoding as YAML
  1.2.2 section 5.2 says, removes a byte order mark and rejects malformed input at its byte; any answer
  that depended on more than a plain UTF-8 decode is marked `decode` (and `error` for the rejection).
  PyYAML, ruamel.yaml and yamlrocks read bytes themselves, so nothing is marked.
- **Errors.** PyYAML and ruamel.yaml name the kind by exception class and give a mark (line and column);
  their reader errors carry only an offset, so those answers have no position. ruamel.yaml raises
  `DuplicateKeyError` and a depth error, which map to `duplicate-key` and `limit`. yamlrocks has parse
  and duplicate-key classes and a line and column. yaml-rs, py-yaml12 and ryaml raise one type with a
  message: the adapter picks the category from the message (`duplicate`, a limit word, otherwise
  `syntax`) and marks every such answer `classification`. It never reads a position out of a message.
- **Dump** is the library's ordinary dump with default settings. Bytes go to the library as `bytes`:
  PyYAML and ruamel.yaml write `!!binary`, yamlrocks writes text, and the other three refuse bytes, which
  the adapter answers `unsupported`. A mapping Python cannot hold (a collection as a key, or keys such as
  `1`, `1.0` and `true` that a dict merges) is `unsupported` too.
- **Patches.**
  - `ruamel-rt` is the combination people use today: the round-trip loader with quotes preserved,
    `jsonpatch.JsonPatch(...).apply(doc)` or `json_merge_patch.merge`, and the round-trip dumper. The
    library never returns bytes for a refused patch, so the adapter reports the input (`unchanged`); a
    document index past the end is the adapter's error (`error`). jsonpatch and jsonpointer raise
    distinct exception types, which map to `patch-invalid`, `patch-path` and `patch-test`; where only the
    message tells them apart (a pointer without a leading slash, a move into its own child) the answer
    is marked `classification`.
  - `yamlrocks` has no pointer language. The adapter walks the pointer through the library's own
    views and lets the library's item assignment and deletion do the edit (`pointer`, `error`,
    `unchanged`). It cannot insert into a sequence, so it declines those cases, and it declines
    `move`, `copy`, `test` and merge patches, which it does not declare.
- **Crashes.** A library that dies on an input (py-yaml12, yaml-rs and PyYAML's C loader are killed by
  SIGSEGV on very deeply nested input; py-yaml12 aborts when an allocation fails) ends the adapter
  process; the kit restarts it and counts the case as failed.
  An exception outside the library's own hierarchy (`RecursionError`, `MemoryError`) is answered `crash`,
  never as a rejection. `memory_limit_mb` is 2048 for every adapter, so an allocation failure or abort
  inside a native library cannot take more than that.

## Known deviations

Each adapter's `deviations.json` lists every case it does not pass, with the comparison's own account of
the difference as the reason. The lists are generated:

```sh
adapters/gokit/record_deviations.py pyyaml-c pyyaml-pure ruamel-safe ruamel-rt yamlrocks yaml-rs py-yaml12 ryaml
```

Read the diff of a regenerated list before accepting it.

## Tests

`tests/test_adapters_python.py` covers the helper (value notation, depth, cycles, encodings, events and
the protocol loop) and the pins; with `KIT_ADAPTER_TESTS=1` it also builds the adapters and checks each
hello: library version, declared capabilities and emulated behaviours. The gate runs the adapters side
by side; its hand-over tier leaves out the safety category (see [the gate](../../docs/gate.md)).
