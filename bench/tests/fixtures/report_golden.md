# YAML library benchmark results

Quick run on 2026-01-01T00:00:00Z.

**SMOKE RESULT.** This run was quick or was taken on a shared machine, so it shows the shape of the comparison and is not a claim about speed.

## Environment

|  |  |
|---|---|
| CPU | Test CPU (4 logical CPUs) |
| Memory | 8.0 GiB |
| OS | Linux (x86_64) |
| Host load (1 min) | 0.5 before, 0.5 after |
| Run time | 1.5 s |
| Kit revision | abc |

**rust** measured with criterion (rustc x, t). Time: tm. Memory: mm.

## Libraries

| Language | Library | Package | Role | Settings |
|---|---|---|---|---|
| rust | `ours` | ours 1.0 | own | defaults |
| rust | `rival` | rival 2.0 | competitor | defaults |
| rust | `other` | other 3.0 | competitor | k: v |

## Summary

Each row of the tables compares our library with the **strongest competitor in that row** (the fastest for time, the least allocated for memory). A competitor that did less than the full task is still the one compared with when it is ahead: it is named with a † and its reason, and a loss to it is a loss. A gap under 5% for time or 2% for memory (12% for .NET) is a tie. Cells without a number say why.

| Language | Task | Workloads | Time win / tie / loss | Memory win / tie / loss | No number | No rival | Weakest row (our time ÷ strongest competitor's) |
|---|---|---|---|---|---|---|---|
| rust | generic-load | 4 | 2 / 0 / 1 | 0 / 2 / 1 | 1 | 0 | w2 2.00× of rival |
| rust | dump | 1 | 1 / 0 / 0 | 0 / 1 / 0 | 0 | 0 | w1 0.90× of other |
| rust | patch | 1 | 0 / 0 / 0 | 0 / 0 / 0 | 1 | 0 | — |

*No number*: our library's answer was wrong, refused or missing. *No rival*: no competitor produced a correct answer for that row, so there is nothing to compare with. A competitor that answered correctly but did less than the full task (†) is a rival.

A time in parentheses names the accepted variant of the expected result when it is not the plain one: *merged* means the library applied `<<` merge keys (a YAML 1.1 extension; YAML 1.2 reads `<<` as an ordinary key), and *bytes-only* means the library gives no value to compare, so its document is judged on the bytes it writes back.

## generic-load

Parse into the library's generic value tree; all documents of a stream.

### generic-load, rust

**Time per operation**

| Workload | Input | `ours` | `other` | `rival` | Ours vs strongest competitor |
|---|---|---|---|---|---|
| `w1` | 1000 B | 50 ns | 120 ns | 100 ns | 0.50× of `rival` ▲ win |
| `w2` | 1.95 KiB | 200 ns | 150 ns | 100 ns | 2.00× of `rival` ▼ loss |
| `w3` | 2.93 KiB | 80 ns | differs | 100 ns | 0.80× of `rival` ▲ win |
| `w4` | 3.91 KiB | differs | — | 100 ns | no number (differs) |

**Allocated memory per operation**

| Workload | Input | `ours` | `other` | `rival` | Ours vs strongest competitor |
|---|---|---|---|---|---|
| `w1` | 1000 B | 900 B in 3 allocs | 800 B in 3 allocs | 400 B in 3 allocs | 2.25× of `rival` ▼ loss |
| `w2` | 1.95 KiB | 100 B in 3 allocs | 100 B in 3 allocs | 100 B in 3 allocs | 1.00× of `rival` ● tie |
| `w3` | 2.93 KiB | 1000 B in 3 allocs | differs | 1000 B in 3 allocs | 1.00× of `rival` ● tie |
| `w4` | 3.91 KiB | differs | — | 1000 B in 3 allocs | no number (differs) |

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="charts/rust-generic-load-time-dark.svg">
  <img alt="generic-load (rust): time, ours against the strongest competitor. Time. Ours ÷ strongest competitor (a † names one that did less than the full task), log scale: left of the 1.0× line is better for us, right of it is a loss. 3 workloads: 2 better, 0 level, 1 worse. w1 0.50× (ours 50 ns vs rival 100 ns); w2 2.00× (ours 200 ns vs rival 100 ns, loss); w3 0.80× (ours 80 ns vs rival 100 ns)." src="charts/rust-generic-load-time-light.svg">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="charts/rust-generic-load-memory-dark.svg">
  <img alt="generic-load (rust): allocated memory, ours against the strongest competitor. Allocated memory. Ours ÷ strongest competitor (a † names one that did less than the full task), log scale: left of the 1.0× line is better for us, right of it is a loss. 3 workloads: 0 better, 2 level, 1 worse. w1 2.25× (ours 900 B vs rival 400 B, loss); w2 1.00× (ours 100 B vs rival 100 B); w3 1.00× (ours 1000 B vs rival 1000 B)." src="charts/rust-generic-load-memory-light.svg">
</picture>

## dump

Write a generic value tree back to YAML text.

### dump, rust

**Time per operation**

| Workload | Input | `ours` | `other` | `rival` | Ours vs strongest competitor |
|---|---|---|---|---|---|
| `w1` | 1000 B | 90 ns | 100 ns | unjudged | 0.90× of `other` ▲ win |

**Allocated memory per operation**

| Workload | Input | `ours` | `other` | `rival` | Ours vs strongest competitor |
|---|---|---|---|---|---|
| `w1` | 1000 B | 1000 B in 3 allocs | 1000 B in 3 allocs | unjudged | 1.00× of `other` ● tie |

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="charts/rust-dump-time-dark.svg">
  <img alt="dump (rust): time, ours against the strongest competitor. Time. Ours ÷ strongest competitor (a † names one that did less than the full task), log scale: left of the 1.0× line is better for us, right of it is a loss. 1 workloads: 1 better, 0 level, 0 worse. w1 0.90× (ours 90 ns vs other 100 ns)." src="charts/rust-dump-time-light.svg">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="charts/rust-dump-memory-dark.svg">
  <img alt="dump (rust): allocated memory, ours against the strongest competitor. Allocated memory. Ours ÷ strongest competitor (a † names one that did less than the full task), log scale: left of the 1.0× line is better for us, right of it is a loss. 1 workloads: 0 better, 1 level, 0 worse. w1 1.00× (ours 1000 B vs other 1000 B)." src="charts/rust-dump-memory-light.svg">
</picture>

## patch

Apply an RFC 6902 patch to a document, atomically, and write the bytes.

### patch, rust

**Time per operation**

| Workload | Input | `ours` | `other` | `rival` | Ours vs strongest competitor |
|---|---|---|---|---|---|
| `w1` | 1000 B | — | — | rejects | no number (unsupported) |

**Allocated memory per operation**

| Workload | Input | `ours` | `other` | `rival` | Ours vs strongest competitor |
|---|---|---|---|---|---|
| `w1` | 1000 B | — | — | rejects | no number (unsupported) |

## Cells without a number

A library that gives a wrong answer, refuses a valid input or cannot be measured gets no time. The reason is the first difference the guard found. *Unjudged* means the harness's two reference parsers disagree about the library's output, so the harness could not decide; it is not a verdict on the library.

| Language | Library | Task | Workload | Status | Reason |
|---|---|---|---|---|---|
| rust | `other` | generic-load | `w3` | differs | at /a/b: got 1, expected 2 |
| rust | `ours` | generic-load | `w4` | differs | byte 3: got x, expected y |
| rust | `rival` | dump | `w1` | unjudged | the two reference parsers disagree at /a |
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
