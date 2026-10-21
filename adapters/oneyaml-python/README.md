# oneyaml-python adapters

The kit adapters for the Python library `oneyaml`, one per backend: `oneyaml-python` (the native
backend) and `oneyaml-python-pure` (the pure-Python backend). Unlike the other Python adapters they do
not pin a published version: the kit does not fetch the library, it builds it from a checkout on the
machine.

## Pointing them at a checkout

```sh
export KIT_ONEYAML_PYTHON_SRC=/path/to/oneyaml-python    # a clone, a snapshot or a worktree
bin/kit fast --adapter oneyaml-python
bin/kit fast --adapter oneyaml-python-pure
```

The build creates a virtual environment of its own for each adapter, outside the checkout (see
[../pykit](../pykit/README.md) for where):

- `oneyaml-python` builds the checkout with maturin (pinned in `adapters/pykit/tools.lock`; a Rust
  toolchain is needed) in the development profile and installs the wheel. The adapter runs with
  `ONEYAML_BACKEND=native`, so a missing native module is an error and not a silent fallback.
- `oneyaml-python-pure` installs the checkout's pure wheel (`tools/build_pure.py`) and runs with
  `ONEYAML_BACKEND=pure`. Nothing is compiled.

The version in the report is the checkout's commit (`devel` when it is not a git checkout), by the
same rule as the other linked adapters (`tools/library-rev.sh`).

## Without the variable

The adapters are **unavailable**, not failed (`requires_env` in `adapter.json`). `bin/kit fast` prints
`SKIPPED, not available: environment variable KIT_ONEYAML_PYTHON_SRC is not set`, the report lists them
under "Not run", and `bin/gate` stays green. They are never built then.

## What they declare

Both adapters run the same code and declare `events`, `error-events`, `value`, `error-category`,
`error-position`, `dump` and every `patch:` operation (`patch:add`, `patch:remove`, `patch:replace`,
`patch:move`, `patch:copy`, `patch:test`, `patch:merge`).

- Reading: events (through the library's internal `events_text`) and values (`oneyaml.loads_all`) with
  the library's default limits; the library's `YAMLError` maps to the kit's category and position.
- Patching: the stream is parsed with `oneyaml.parse` and the patch goes to `Document.patch` or
  `Document.merge_patch`. Nothing is emulated: the library reads the pointers, applies the operations
  all or nothing and names the kit's category in its `PatchError`. After a failed patch the adapter
  reports the document's own text, which the kit checks against the input.
- Dumping: the case's value is written with the library's own `dumps` and its default settings; byte
  strings go in as `bytes` and list or mapping keys as tuples and `FrozenMapping`s.

The hello's `info` names the package version, the backend that imported (`native` or `pure`) and the
engine version the package reports.
