# The gate

The gate is the kit's own check: run it before you hand over a change, and CI runs the full
version of it on every push and pull request. This page says what each tier runs and how to read
its output.

```sh
bin/gate                    # hand-over gate: the kit core and the areas your change touched
bin/gate --full             # everything, every adapter (CI runs this)
bin/gate --plan             # show what would run, and stop
bin/gate --areas bench-go   # named areas instead of choosing by changes
bin/gate --jobs 2           # areas to run side by side
bin/gate --budget 150       # the hand-over time budget, in seconds
```

`bin/gate --help` lists every option. `KIT_OUT` moves the output directory (default `./out`).

## Tiers

The gate has two tiers.

### Hand-over gate

`bin/gate` always runs the **kit core**:

- the corpus pins,
- the kit's own tests,
- the public-text check,
- `bin/kit links`, which fails on a relative link in a top-level Markdown file (the README, CONTRIBUTING.md, SECURITY.md, ...) or under `docs/` whose file or
  heading does not exist (absolute URLs are not fetched),
- `bin/kit blobs`, which refuses tracked executables, core dumps and files over 1 MiB.

It adds an area only when the files that differ from `main` touch it, and then runs only that
area's quick steps: format and lint checks, unit and self-tests, and for a benchmark harness a
smoke run over three small workloads. Renames count as a deletion and an addition, so the area a
file left is checked too.

Two changes reach further than their own area:

- A change to the kit's code or cases also runs the reference adapter (the Rust one), because the
  kit's code and cases can change what every adapter scores.
- A change to the benchmark driver also smoke-tests the Rust harness.

The hand-over gate has a time budget of 150 seconds (`--budget`) and fails when a run exceeds it,
which keeps the tier inside its three-minute limit. When it fails on time, the fix is a faster
step or moving the step to the full tier.

### Full gate

`bin/gate --full` runs every area and every step, including the slow ones:

- the end-to-end benchmark pipeline tests,
- the quick benchmark run over every workload,
- the plain-build lint of the Rust harness.

It has no budget.

## Areas

An area is a part of the repository with its own checks:

| Area | What it covers |
|---|---|
| `core` | the kit core steps above, run by every hand-over gate; every file outside the other areas belongs to it |
| `rust`, `go`, `csharp`, ... | the adapters of one language |
| `bench` | the benchmark driver, report and workloads |
| `bench-rust`, `bench-go`, `bench-csharp` | one benchmark harness each, so a change to one harness never builds another |

Areas run side by side: as many as the memory available to the gate holds (the tightest cgroup
limit, else the available memory), at most four. The first output line says how many and why.
`--jobs` sets the number instead.

The count rests on a fixed peak-memory estimate per area. Each step's largest process is measured
by the kernel while it runs and recorded as `peak_mib` in `out/gate/summary.json`. When an area's
largest process is above its estimate, the gate prints a warning and lists it under
`estimate_warnings`. The figure is the largest single process, a floor under the area's real peak,
so a warning is proof the estimate is too low and silence is not proof that it is right. The
warning does not fail the run.

## Output

Each step logs under `out/gate/` and shows its output only when it fails.

Before any step, the gate prints the revision it checked, and `out/gate/summary.json` records it:

- HEAD, the base and the merge base,
- a fingerprint of the change against the merge base: the binary diff, plus the path and content
  hash of every untracked file,
- whether the tree is dirty.

Two runs with the same fingerprint checked the same change, so their results can be compared.

## Timing and bounded cases

A bounded (safety) case's time limit is a hang guard set far above the time any reader that
answers needs, so a verdict does not depend on how fast the machine is. Its memory bound is the
real check. The rule is in the [case format](case-format.md#bounded-outcomes-safety).

## In CI

CI ([`.github/workflows/gate.yml`](../.github/workflows/gate.yml)) runs the full gate as one
job per area (`bin/gate --full --areas <area>`), each on its own runner with its own caches, so
the slowest area sets the time rather than the sum of them.

A separate job runs the `blobs` check over the whole fetched history (`bin/kit blobs --all`:
every branch and tag), not just the commits of one push. A large file or a core dump that is
added and then deleted still ships in the history, and a cancelled run must not leave a pushed
commit unchecked.
