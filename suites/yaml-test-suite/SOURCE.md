# Vendored yaml-test-suite data

- **Upstream:** https://github.com/yaml/yaml-test-suite
- **Release tag:** `data-2022-01-17` (the scored baseline)
- **Revision:** `6e6c296ae9c9d2d5c4134b4b64d01b29ac19ff6f`
- **Archive:** https://github.com/yaml/yaml-test-suite/archive/refs/tags/data-2022-01-17.tar.gz
- **Archive SHA-256:** `ccae49c1d010d27c20d315969a97752f95b14642fc1e73755db4eb2df4bb0256`
- **Licence:** MIT, see `LICENSE` in this directory. The data archive carries no licence
  file, so `LICENSE` was fetched from the matching source tag
  (https://raw.githubusercontent.com/yaml/yaml-test-suite/v2022-01-17/License).

## What is here

`data-2022-01-17/` holds every test-case directory of the archive, byte for byte:
350 top-level directories, 402 cases (some directories hold numbered sub-cases),
94 of them error cases (an `error` file is present). Per case: `===` (title),
`in.yaml`, `test.event`, and where present `error`, `in.json`, `out.yaml`,
`emit.yaml`, `lex.token`.

Not vendored: the archive's `name/` and `tags/` directories, which only hold
symbolic links back to the case directories.

The files are never edited. Where an upstream expectation is wrong, the corrected
expectation lives in `corrections.json` next to this file, with the reason and a
specification citation, and the kit scores both.

The digest of the whole tree is pinned in `tests/pins.json`, so any change to the
data fails the kit's own tests.

## Tracking `main`

The `main` branch is informational only and is not vendored yet. To add it, put
its data beside `data-2022-01-17/` and register it in
`tools/oneyaml_kit/cases.py`; it is never part of the scored baseline.

## Updating

Replace the data directory with a newer release, update this file, regenerate the
pins (`bin/kit pins --write`), and check the diff of the pins and the corrections.
