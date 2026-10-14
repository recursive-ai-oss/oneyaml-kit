# YAML library benchmark results

**SMOKE RESULT, NOT PUBLISHABLE.** Quick run on 2026-01-01T00:00:00Z.

Why this run is not publishable:

- the run was a quick run; only a full run can be published

## Environment

|  |  |
|---|---|
| CPU | Test CPU (4 logical CPUs) |
| Memory | 8.0 GiB |
| OS | Linux (x86_64) |
| CPU governor, boost | performance, disabled |
| Pinned to CPU | no |
| Host load (1 min) | 0.5 before, 0.5 after |
| Foreign busy cores | p95 0.1, max 0.1 (10 samples) |
| Run time | 1.5 s, 0 harness restarts |
| Kit revision | abc |

**rust harness** `h 1`, rustc x, target t, flags {'opt': 3}. Warm-up 1 ms, 5 samples of about 1 ms; tm. Memory: mm.

## Libraries

| Language | Library | Crate/package | Role | Settings |
|---|---|---|---|---|
| rust | `ours` | ours 1.0 | own | defaults |
| rust | `rival` | rival 2.0 | competitor | defaults |
| rust | `other` | other 3.0 | competitor | k: v |

## Summary

Each row of the tables compares our library with the **best competitor in that row** (the fastest for time, the smallest peak for memory). A gap inside the measurement noise (at least 2% for time, 1% for memory) is a tie. Cells without a number say why.

| Language | Task | Workloads | Time win / tie / loss | Memory win / tie / loss | No number | No rival | Weakest row (our time ÷ best competitor's) |
|---|---|---|---|---|---|---|---|
| rust | generic-load | 4 | 2 / 0 / 1 | 0 / 2 / 1 | 1 | 0 | w2 2.00× of rival |
| rust | patch | 1 | 0 / 0 / 0 | 0 / 0 / 0 | 1 | 0 | — |

*No number*: our library's answer was wrong, refused or missing. *No rival*: no competitor produced a correct answer for that row, so there is nothing to compare with.

A time in parentheses names the accepted variant of the expected result when it is not the plain one: *merged* means the library applied `<<` merge keys (a YAML 1.1 extension; YAML 1.2 reads `<<` as an ordinary key), *restyled* means an edited value was written in quotes where the original plain style could hold it, and *bytes-only* means the library has no data model, so its document is judged on the bytes it writes back.

## generic-load

Parse into the library's generic value tree; all documents of a stream.

### generic-load, rust

**Time per operation**

| Workload | Input | `ours` | `other` | `rival` | Ours vs best competitor |
|---|---|---|---|---|---|
| `w1` | 1000 B | 50 ns ±2% | 120 ns ±1% | 100 ns ±1% | 0.50× of `rival` ▲ win |
| `w2` | 1.95 KiB | 200 ns ±0% | 150 ns ±1% | 100 ns ±1% | 2.00× of `rival` ▼ loss |
| `w3` | 2.93 KiB | 80 ns ±1% | differs | 100 ns ±1% | 0.80× of `rival` ▲ win |
| `w4` | 3.91 KiB | differs | — | 100 ns ±1% | no number (differs) |

**Peak / allocated heap per operation**

| Workload | Input | `ours` | `other` | `rival` | Ours vs best competitor |
|---|---|---|---|---|---|
| `w1` | 1000 B | 900 B / 1.76 KiB | 800 B / 1.56 KiB | 400 B / 800 B | 2.25× of `rival` ▼ loss |
| `w2` | 1.95 KiB | 100 B / 200 B | 100 B / 200 B | 100 B / 200 B | 1.00× of `rival` ● tie |
| `w3` | 2.93 KiB | 1000 B / 1.95 KiB | differs | 1000 B / 1.95 KiB | 1.00× of `rival` ● tie |
| `w4` | 3.91 KiB | differs | — | 1000 B / 1.95 KiB | no number (differs) |

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="charts/rust-generic-load-time-dark.svg">
  <img alt="generic-load (rust): time, ours against the best competitor. Best competitor ÷ ours, time; more than 1.0× is a win, bars left of the dashed line are losses. 3 workloads: 2 faster, 0 level, 1 slower. w1 2.00× (ours 50 ns vs rival 100 ns); w2 0.50× (ours 200 ns vs rival 100 ns, loss); w3 1.25× (ours 80 ns vs rival 100 ns)." src="charts/rust-generic-load-time-light.svg">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="charts/rust-generic-load-memory-dark.svg">
  <img alt="generic-load (rust): peak heap, ours against the best competitor. Best competitor ÷ ours, peak heap; more than 1.0× is a win, bars left of the dashed line are losses. 3 workloads: 0 faster, 2 level, 1 slower. w1 0.44× (ours 900 B vs rival 400 B, loss); w2 1.00× (ours 100 B vs rival 100 B); w3 1.00× (ours 1000 B vs rival 1000 B)." src="charts/rust-generic-load-memory-light.svg">
</picture>

## patch

Apply an RFC 6902 patch to a document, atomically, and write the bytes.

### patch, rust

**Time per operation**

| Workload | Input | `ours` | `other` | `rival` | Ours vs best competitor |
|---|---|---|---|---|---|
| `w1` | 1000 B | — | — | rejects | no number (unsupported) |

**Peak / allocated heap per operation**

| Workload | Input | `ours` | `other` | `rival` | Ours vs best competitor |
|---|---|---|---|---|---|
| `w1` | 1000 B | — | — | rejects | no number (unsupported) |

## Cells without a number

A library that gives a wrong answer, refuses a valid input or cannot be measured gets no time. The reason is the first difference the guard found.

| Language | Library | Task | Workload | Status | Reason |
|---|---|---|---|---|---|
| rust | `other` | generic-load | `w3` | differs | at /a/b: got 1, expected 2 |
| rust | `ours` | generic-load | `w4` | differs | byte 3: got x, expected y |
| rust | `rival` | patch | `w1` | rejects | boom \| pipe |

### Unsupported tasks

| Language | Library | Task | Why |
|---|---|---|---|
| rust | `other` | generic-load | unsupported |
| rust | `ours` | patch | unsupported |

## Workloads

| Workload | Kind | Input | Documents | Source | Licence |
|---|---|---|---|---|---|
| `w1` | generated | 1000 B | 1 | g | CC0-1.0 |
| `w2` | generated | 1.95 KiB | 1 | g | CC0-1.0 |
| `w3` | generated | 2.93 KiB | 1 | g | CC0-1.0 |
| `w4` | generated | 3.91 KiB | 1 | g | CC0-1.0 |

Method, task definitions and the correctness guard are described in docs/benchmarks.md.
