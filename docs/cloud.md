# Timed benchmarks on EC2 spot instances

Publishable (full-mode) numbers come from instances started for the run and shut down
afterwards. `bin/bench-cloud` does the whole round: pack the inputs, start one spot instance
per shard, follow their logs, collect the results, merge them with `bin/bench merge`, and
terminate everything. It needs Python 3.10 or newer and the AWS command line tool; it adds no
Python packages.

```
bin/bench-cloud setup                       # once: the private bucket
bin/bench-cloud image                       # bake the machine image, or reuse the current one
bin/bench-cloud run --shards auto           # a full run on as many instances as pay off
bin/bench-cloud run --shards auto --dry-run # only the plan, the estimates and the cost check
bin/bench-cloud run --shards 2 --mode quick --lang go    # a small end-to-end check
bin/bench-cloud spread --types A,B          # compare the run-to-run spread of instance types
bin/bench-cloud cleanup                     # terminate and delete anything tagged that is left
```

Numbers from a cloud run are still labelled by the harness's own publishability rule
(`docs/benchmarks.md`). A quick run proves the pipeline; only a full run can be published.

## Settings

The account settings are a `KEY=value` file outside the repository, named by
`KIT_CLOUD_CONFIG` (default `~/.config/oneyaml-kit/cloud.env`). It holds names, never
credentials:

| Key | Meaning |
|---|---|
| `KIT_CLOUD_REGION` | region of the instances and the bucket |
| `KIT_CLOUD_BUCKET` | private bucket for inputs, logs and results |
| `KIT_CLOUD_BASE_AMI` | base image the machine image is built from (Ubuntu 24.04, x86-64) |
| `KIT_CLOUD_INSTANCE_TYPE` | default instance type |
| `KIT_CLOUD_COST_CAP` | estimated-cost limit in dollars (default 25) |
| `KIT_CLOUD_SPOT_VCPU_QUOTA` | spot vCPU quota; limits the number of instances |

Credentials come from the AWS command line tool's usual sources and stay on the local
machine. The default network of the region is used.

## What happens in a run

1. **Plan.** `bin/bench plan --shards N --mode M` (with the same `--lang`, `--task`,
   `--library` and `--workload` filters the shards get, since the plan digest covers them)
   splits the selection into shards of whole comparison rows. Its `estimated_cost` is a
   relative figure, so the launcher turns it into seconds with a calibration per mode
   (`SECONDS_PER_COST` in `plan.py`, see [Calibration](#calibration)). With `--shards auto` it
   asks for plans of 1, 2, 3, ... shards (up to what the harness can split and the spot quota
   allows) and takes the count with the shortest estimated wall-clock time whose estimated cost
   stays under the cap; an extra instance only counts when it saves at least 3% of the
   wall-clock time. The candidates are printed. `--dry-run` stops after this and the cost check.
2. **Cost guard.** Estimated cost = live spot price x instances x (start-up time + shard
   estimate), plus the conformance job. A run above the cap (`--cost-cap` raises it) is refused
   before anything starts. An on-demand fallback that would take the total over the cap is
   refused too. The summary prints the estimate again at the end, from the lifetimes of the
   instances and the price of the life cycle each one used.
3. **Pack.** `git archive` of the kit's HEAD (`bin`, `bench`, `tools`, `suites`, `adapters`) and
   of each library source named by `KIT_ONEYAML_GO_SRC` and `KIT_ONEYAML_CSHARP_SRC`, with the
   revisions recorded in `cloud-run.json` (and a note when the checkout has uncommitted
   changes, which an archive does not contain). `--kit-tree` packs the kit's working tree
   instead, for a change that is not committed; the kit is then labelled `+modified`. The Rust
   harness takes its library from crates.io at a pinned version, so it has no source to pack.
   The tarballs go into one input object in the bucket.
4. **Launch.** One spot instance per shard, plus one for the conformance run, all of one type,
   started in parallel. Each instance boots from the machine image, and its boot script does
   everything else.
5. **Follow.** The instance uploads its log every few seconds; the launcher prints new lines
   with the job label.
6. **Collect, merge, tear down.** When a job reports, its results are downloaded and the
   instance is told to terminate. `bin/bench merge` runs on the shards while the instances
   finish terminating, and the conformance results are filed beside it.

### What counts as a successful shard

A shard is never publishable on its own (the harness says so in its result), and the harness
exit status does not report that: `bin/bench run --shard` exits 0 when the harness ran and its
list of known deviations matched, and 1 when it did not (or when generated text fails the public
text check); 2 when the harness refused to start. A cell that crashed or stalled does **not**
change the exit status: it appears as a cell with status `error`. So after each shard the
instance checks three things, and the shard counts as a success only if all hold:

- the exit status is 0;
- `results.json` exists, is readable and carries `run.shard`;
- no cell has status `error`. `differs`, `rejects` and `unsupported` are results, not failures.

Anything else fails the shard (exit codes 23 and 24 are the checks'). A failed shard is not
retried, since it ran: the retries are for lost or unstartable instances. Its output is still
downloaded to `jobs/<n>/` for inspection and never merged. `bin/bench merge` is called without
`--allow-incomplete`, so a missing shard refuses the merge; `bin/bench-cloud run
--allow-incomplete` passes it through, merges the shards that succeeded, and still exits
non-zero (the merged result is never publishable).

The kit revision comes out of an archive as unknown (the harness reads it from git), so the
check script fills `environment.kit_revision` from the packed revision when it is empty. The
instance type needs no patching: the boot script exports `KIT_INSTANCE_TYPE` and the
harness's environment record reads it, which is what `bin/bench merge` compares.

### Conformance

`bin/kit run --slow --adapter <library> --out DIR` (all cases, the slow ones included, no
filter) runs on one more instance of the same type, for every library of ours in the selected
languages whose source is available (`oneyaml-go` needs `KIT_ONEYAML_GO_SRC`, `oneyaml-csharp`
needs `KIT_ONEYAML_CSHARP_SRC`, `oneyaml-rust` builds from the kit). It is on by default
(`--no-conformance` turns it off) and runs beside the shards, so it adds no wall-clock time.
It is not timed. A conformance run that exits non-zero (for example a stale entry in the
library's list of known deviations) fails the run, but the shards are still merged. Its result
is filed as `results/conformance/<library>.json` beside the merged
`results/bench/results.json`, the layout `bin/bench readme --results <run>/results` reads.

### Machine image

`bin/bench-cloud image` bakes an image with the Go, .NET and Rust toolchains, the system
packages (including valgrind for the instruction counters), and warm module, package and
build caches for the three harnesses, built at the path a run uses so a run recompiles only
what changed. The image is found by a digest of the pinned toolchain versions
(`toolchains.py`), the base image, the bake script, and the lock files of the three harnesses.
It is rebuilt only when that digest changes. Boot-to-measuring is what the image buys: see
[Timings](#timings).

### Where the time goes

The instance records a clock mark at each step (kernel boot, script start, setup, inputs,
cache warm-up, build, first measurement, end) and reports them with the result. `run` prints
them per shard and `cloud-run.json` keeps them, together with the wall-clock phases of the
launcher (pack, upload, jobs, merge, teardown).

## Instances are locked down

- No SSH key, no inbound rule (the run's security group has none), no IAM role: an instance
  has no credentials of any kind. IMDSv2 is required.
- Inputs and outputs go through presigned URLs for single objects, each for one method,
  placed in the user data. They are signed with temporary credentials that expire shortly
  after the run's deadline, so a leaked URL stops working.
- The bucket blocks public access and expires objects after a day. `run` deletes its own
  objects at the end.
- Everything a run creates (instances, spot requests, volumes, security group, objects) is
  tagged with the run id.

## Shutdown is guaranteed three ways

1. The instance is launched with shutdown behaviour *terminate*. Its boot script powers off
   when it finishes and on any error, after uploading what it has.
2. A lifetime limit (`shutdown -h +M`) is the first command of the boot script, before any step
   that could hang. M is the shard's estimated run time with slack, plus start-up time.
3. The launcher terminates every instance tagged with the run id on success, failure, time
   limit, Ctrl-C, SIGTERM and SIGHUP, then deletes the run's security group and objects. A
   second signal during the teardown is ignored.

`bin/bench-cloud cleanup [--run ID]` finds tagged leftovers (instances, spot requests,
security groups, launch templates, volumes, stored objects older than the bucket's expiry),
removes them, and prints what the account still holds. `run` calls it for its own run id at
the end and fails when anything tagged is still running.

Bare metal instances take several minutes to finish terminating. An instance that has started
shutting down no longer runs and is no longer billed, so `run` waits only a short while for
the final state and reports the rest as pending; its security group is deleted by the next
`cleanup`, which waits as long as needed.

## Spot interruptions

A shard whose instance is lost (interrupted, or powered off by its lifetime limit without a
result) or cannot be started for lack of capacity is started again on a new spot instance, up
to `--spot-retries` more times (default 2). After that one on-demand instance of the same
type is tried (`--no-on-demand` turns this off). A run never mixes instance types. A shard
that ran and failed is not retried: that is a result. `cloud-run.json` records every
attempt, and the summary says which shards ran on demand.

## Measurement fidelity

The boot script, on every instance:

- sets the `performance` governor where the platform exposes one, and holds the CPU
  latency request at zero so the cores stay out of deep idle states;
- stops the stock timers and services that wake up on their own;
- reads the cached toolchains into memory while the inputs download (a volume made from an
  image loads blocks on first use);
- runs one shard per instance, with the harness pinned to a CPU (`--pin`, default 2);
- records the instance type, availability zone, life cycle, kernel, governor and CPU count.

**How the instance type is recorded.** The boot script exports `KIT_INSTANCE_TYPE` (also
`KIT_INSTANCE_AZ` and `KIT_INSTANCE_LIFECYCLE`) from the instance metadata before the
harness starts; the harness's environment record reads `KIT_INSTANCE_TYPE` into
`environment.instance_type`, and `bin/bench merge` compares that field across shards.

### Choosing the instance type

Measured with `bin/bench-cloud spread`: five quick runs of the Go and Rust harnesses
(`generic-load` and `dump`, every workload and library, 335 cells) on one spot instance of
each candidate, pinned to one CPU. "Between runs" is the spread of a cell's median over the
five runs; "within" is the median absolute deviation inside a run.

| Type | CPU | Governor seen by the harness | Between-run spread, median / 90th percentile / worst cell | Within a run | Time against metal | Spot price |
|---|---|---|---|---|---|---|
| c7i.metal-24xl | Xeon 8488C, 96 vCPUs | `performance` | 1.11% / 3.62% / 7.95% | 0.41% | 1.00 | $0.93/h |
| c7i.8xlarge | Xeon 8488C, 32 vCPUs | unreadable | 0.80% / 3.48% / 7.93% | 0.43% | 0.86 | $0.33/h |
| c7a.8xlarge | EPYC 9R14, 32 vCPUs | unreadable | 1.67% / 6.49% / 22.35% | 0.61% | 0.71 | $0.32/h |

(Load on the instances was a pinned harness only; one sample of one afternoon in one region, a
smoke measurement, not a bound.)

The virtualised sizes are no steadier on the median (one is, one is not), reach the first
cell about as fast (19 to 21 s from kernel boot, in all three), and ran this work 14% and 29%
faster than the metal size, which is a difference of speed, not of quality. But a guest on Nitro cannot read or set a CPU frequency governor, and the harness's
publishable rule needs a readable `performance` governor (`docs/benchmarks.md`). A run on
them would always be labelled smoke. The metal size passes that rule, has no neighbours on the
host, and its spread is within a point of the others. So **`c7i.metal-24xl` is the default**.
The costs of that choice: the spot pool is thinner (a shard often needs a retry or the
on-demand fallback at about $4.28/h (in the three real quick runs, 8 of 9 instances ran on demand because
spot metal capacity was refused)), and termination takes minutes (not billed, and handled
as described above). `--instance-type` selects another size for experiments.

## Timings

Boot-to-measuring is the time from the launch request to the first measured cell. It is
printed per job, split into API to kernel boot, kernel to boot script, setup, inputs, cache
warm-up and build. On the baked image, on `c7i.metal-24xl`, in the Go quick run with the
library source linked in: 39 to 43 s (18 to 23 s to the kernel, 11 to 13 s to the boot script,
3 s setup, 0.3 s inputs, 4 to 5 s cache warm-up, 1 to 1.5 s build). The harnesses are built
before the driver starts, outside its load sampling; a build inside the run showed up as
7 to 8 busy cores of foreign load and made every shard fail the harness's load limit.

## Calibration

The planner's cost is relative, so seconds come from measurement. Quick, Go, two shards of 70.7
and 70.2 million units: 59.8 and 76.4 s of measuring (and 60.0 and 74.2 s in the second
run), about 9.7e-7 s per unit; about 0.33 s per timed cell, almost all of it per-cell fixed
cost. Full mode is not measured yet: a full cell adds a 300 ms warm-up and 31 samples of about
50 ms, so the estimate is 2.0e-6 s per unit (about 2.4 s a cell; the full Go selection has 1.31
times the timed cells of the quick one for 4.9 times the cost units). Refit
`SECONDS_PER_COST["full"]` from the first full run; the build allowance for Rust and C#
(30 s each) and the conformance allowance (120 s per library) are assumptions as well.

What `--shards auto --dry-run` picks with these figures, at the spot price of the day
($0.93 per hour for `c7i.metal-24xl`; an on-demand fallback costs about 4.6 times as much):

| Selection | Pick | Estimated wall-clock | Estimated cost |
|---|---|---|---|
| full, `--lang go` (3 or 4 shards tie; the biggest workload is one row of 484 s) | 3 shards + conformance | 554 s (9 min) | $0.45 |
| full, all languages (limited by the quota: 4 shards + conformance) | 4 shards + conformance | 1506 s (25 min) | $1.66 |

One instance would take 1444 s (go) and 5627 s (all languages).

## Files

| Path | Purpose |
|---|---|
| `bin/bench-cloud` | the launcher |
| `bench/oneyaml_bench/cloud/cli.py` | commands, cost guard, shard-to-job mapping |
| `bench/oneyaml_bench/cloud/runner.py` | the run: threads per shard, retries, teardown, merge hook |
| `bench/oneyaml_bench/cloud/bootstrap.py` | the scripts instances run at boot |
| `bench/oneyaml_bench/cloud/image.py`, `toolchains.py` | the machine image and its digest |
| `bench/oneyaml_bench/cloud/aws.py`, `sigv4.py` | the only code that talks to AWS; presigned URLs |
| `bench/oneyaml_bench/cloud/plan.py`, `pack.py`, `cleanup.py`, `config.py` | planning and calibration, packing, cleanup, settings |
| `bench/tests/test_cloud.py` | tests with the AWS layer faked |
