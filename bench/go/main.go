// In-process benchmark harness for Go YAML libraries.
//
// `run` measures every (library, task, workload) cell that the workload set defines, after
// the correctness guard has accepted what the library produced, and writes one JSON line per
// cell. See docs/benchmarks.md for the protocol and the result format.
package main

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"sort"
)

type args struct {
	cmd       string
	workloads string
	out       string
	mode      string
	libraries []string
	tasks     []string
	only      []string
	skip      []string
}

func parseArgs(argv []string) (*args, error) {
	if len(argv) == 0 {
		return nil, fmt.Errorf("usage: oneyaml-bench-go <run|once|list> [options]")
	}
	a := &args{cmd: argv[0], workloads: "workloads", mode: "quick"}
	for i := 1; i < len(argv); i++ {
		k := argv[i]
		val := func() (string, error) {
			i++
			if i >= len(argv) {
				return "", fmt.Errorf("%s needs a value", k)
			}
			return argv[i], nil
		}
		v, err := val()
		if err != nil {
			return nil, err
		}
		switch k {
		case "--workloads":
			a.workloads = v
		case "--out":
			a.out = v
		case "--mode":
			a.mode = v
		case "--library":
			a.libraries = append(a.libraries, v)
		case "--task":
			a.tasks = append(a.tasks, v)
		case "--workload":
			a.only = append(a.only, v)
		case "--skip":
			a.skip = append(a.skip, v)
		default:
			return nil, fmt.Errorf("unknown option %s", k)
		}
	}
	return a, nil
}

func cellName(lib string, t Task, w string) string { return lib + "/" + string(t) + "/" + w }

// out writes JSON lines, each flushed as it is written, so the driver can name the cell that
// killed the process.
type lineWriter struct{ f *os.File }

func (l lineWriter) write(v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	_, err = l.f.Write(append(b, 0x0a))
	return err
}

func harnessRecord(a *args, cfg Config, libs []Library) map[string]any {
	var ls []map[string]any
	for _, l := range libs {
		ls = append(ls, map[string]any{"id": l.ID(), "package": l.Package(), "version": moduleVersion(l.Package()),
			"role": l.Role(), "settings": l.Settings()})
	}
	gogc := os.Getenv("GOGC")
	if gogc == "" {
		gogc = "100"
	}
	flags := map[string]any{"GOMAXPROCS": runtime.GOMAXPROCS(0), "GOGC": gogc, "trimpath": true}
	if bi, ok := debug.ReadBuildInfo(); ok {
		for _, s := range bi.Settings {
			if s.Key == "GOAMD64" || s.Key == "CGO_ENABLED" {
				flags[s.Key] = s.Value
			}
		}
	}
	return map[string]any{
		"type": "harness", "name": "oneyaml-bench-go", "version": "0.1.0", "language": "go",
		"runtime": runtime.Version(), "target": runtime.GOOS + "/" + runtime.GOARCH,
		"flags": flags, "mode": a.mode,
		"config": map[string]any{"warmup_ms": cfg.Warmup.Milliseconds(), "samples": cfg.Samples,
			"sample_target_ms": cfg.SampleTarget.Milliseconds()},
		"timer":          "testing.Benchmark, chunks of prepared iterations, median and median absolute deviation of the samples",
		"memory":         memoryMethod,
		"selftest_build": selftestBuild,
		"libraries":      ls,
	}
}

// memoryMethod names how the memory figures are taken; the report prints it.
const memoryMethod = "runtime.MemStats, sampled"

func oneCell(lib Library, task Task, w *Workload, cfg Config) map[string]any {
	rec := map[string]any{"type": "cell", "library": lib.ID(), "task": string(task), "workload": w.ID,
		"input_bytes": len(w.Input)}
	put := func(status, detail string) { rec["status"], rec["detail"] = status, detail }

	var runner Runner
	var why string
	prep := func() (p any) {
		defer func() {
			if r := recover(); r != nil {
				p = fmt.Sprint("panic while preparing: ", r)
			}
		}()
		runner, why = lib.Runner(task, w)
		return nil
	}()
	if prep != nil {
		put("error", prep.(string))
		return rec
	}
	if runner == nil {
		put("unsupported", why)
		return rec
	}
	// Guard first: run the task once and judge the answer. A wrong answer gets no number.
	var prod Product
	var err error
	var failing *FailedPatch
	func() {
		defer func() {
			if r := recover(); r != nil {
				err = fmt.Errorf("panic: %v", r)
			}
		}()
		prod, err = runner.Check()
		failing = runner.CheckFailing()
	}()
	v := judge(task, w, prod, err, failing, twoReferees)
	put(v.Status, v.Detail)
	if v.Status != "ok" {
		return rec
	}
	rec["variant"] = v.Variant

	t := timeRunner(runner, cfg)
	if t.Panic != nil {
		put("error", fmt.Sprint("panic while timing: ", t.Panic))
		delete(rec, "variant")
		return rec
	}
	m := memoryPass(runner, avgRunsFor(t, cfg))
	if d := agree(t, m); d != "" {
		put("error", d)
		delete(rec, "variant")
		return rec
	}
	rec["time"] = map[string]any{"iters_per_sample": t.ItersPerSample, "samples": len(t.SamplesNS),
		"median_ns": t.Median(), "mad_ns": t.MAD(), "min_ns": t.SamplesNS[0],
		"max_ns": t.SamplesNS[len(t.SamplesNS)-1], "samples_ns": t.SamplesNS}
	// Peak comes from the memory pass. The bytes and calls are testing.B's averages over all the
	// timed runs, the largest sample, which the memory pass has just confirmed.
	rec["memory"] = map[string]any{"method": memoryMethod, "peak_bytes": m.PeakBytes,
		"allocated_bytes": uint64(math.Round(t.BytesPerOp)), "allocations": uint64(math.Round(t.AllocsPerOp))}
	return rec
}

func selectLibs(want []string) []Library {
	var out []Library
	for _, l := range allLibraries() {
		if len(want) == 0 || contains(want, l.ID()) {
			out = append(out, l)
		}
	}
	return out
}

func run(a *args) error {
	cfg := quickConfig()
	if a.mode == "full" {
		cfg = fullConfig()
	}
	f, err := os.Create(a.out)
	if err != nil {
		return err
	}
	defer f.Close()
	out := lineWriter{f}
	ids, err := workloadIDs(a.workloads, a.only)
	if err != nil {
		return err
	}
	libs := selectLibs(a.libraries)
	if err := out.write(harnessRecord(a, cfg, libs)); err != nil {
		return err
	}
	for _, id := range ids {
		w, err := loadWorkload(filepath.Join(a.workloads, id))
		if err != nil {
			return err
		}
		for _, task := range w.Tasks {
			if len(a.tasks) > 0 && !contains(a.tasks, string(task)) {
				continue
			}
			for _, lib := range libs {
				name := cellName(lib.ID(), task, w.ID)
				if contains(a.skip, name) {
					continue
				}
				if err := out.write(map[string]any{"type": "begin", "cell": name}); err != nil {
					return err
				}
				if err := out.write(oneCell(lib, task, w, cfg)); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

// once runs one iteration of one cell, with no guard and no clock.
func once(a *args) error {
	if len(a.libraries) != 1 || len(a.tasks) != 1 || len(a.only) != 1 {
		return fmt.Errorf("once needs one --library, one --task and one --workload")
	}
	w, err := loadWorkload(filepath.Join(a.workloads, a.only[0]))
	if err != nil {
		return fmt.Errorf("workload %s: %v", a.only[0], err)
	}
	libs := selectLibs(a.libraries)
	task, _ := parseTask(a.tasks[0])
	if len(libs) != 1 {
		return fmt.Errorf("no library %s", a.libraries[0])
	}
	r, why := libs[0].Runner(task, w)
	if r == nil {
		return fmt.Errorf("unsupported: %s", why)
	}
	b := r.Prepare(1)
	b.Run()
	return nil
}

func list() error {
	var ids []string
	for _, l := range allLibraries() {
		ids = append(ids, fmt.Sprintf("%s %s %s %s", l.ID(), l.Package(), moduleVersion(l.Package()), l.Role()))
	}
	sort.Strings(ids)
	for _, s := range ids {
		fmt.Println(s)
	}
	return nil
}

func main() {
	a, err := parseArgs(os.Args[1:])
	if err == nil {
		switch a.cmd {
		case "run":
			err = run(a)
		case "once":
			err = once(a)
		case "list":
			err = list()
		default:
			err = fmt.Errorf("unknown command %s", a.cmd)
		}
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "oneyaml-bench-go:", err)
		os.Exit(2)
	}
}
