using System.Diagnostics;
using System.Runtime;
using BenchmarkDotNet.Attributes;
using BenchmarkDotNet.Configs;
using BenchmarkDotNet.Engines;
using BenchmarkDotNet.Jobs;
using BenchmarkDotNet.Loggers;
using BenchmarkDotNet.Running;
using BenchmarkDotNet.Toolchains.InProcess.Emit;

namespace OneYamlKit.Bench;

/// <summary>How long to warm up and how many samples to take.
///
/// The runtime runs with its defaults: tiered compilation, quick JIT and dynamic PGO on, as a user's program
/// does. A method then goes through unoptimised code, instrumented code and finally optimised code that the
/// profile shaped, and each step waits for a timer and a call count. The warm-up therefore runs the cell until the
/// number of methods the JIT has compiled stops changing for a whole settle window (the runtime waits 100 ms
/// without new unoptimised code before it counts calls, so a window of 250 ms spans one step), and only then
/// calibrates and samples.</summary>
public sealed record Config(
    int WarmupMinMs, int WarmupMaxMs, int SettleWindowMs, int SettleWindows,
    int Samples, int MinSamples, int SampleTargetMs, long MaxPreparedBytes, int MaxIters, int MaxCellMs)
{
    public static Config Quick => new(0, 4_000, 250, 1, 5, 5, 1, 64L << 20, 10_000, 4_000);
    public static Config Full => new(300, 12_000, 250, 2, 31, 30, 50, 512L << 20, 1_000_000, 20_000);
}

/// <summary>What the warm-up did: how long it ran, whether the compiled-method count settled before the limit,
/// and how many methods the JIT compiled while it ran.</summary>
public sealed record WarmupResult(double Ms, bool Settled, long Compiled);

/// <param name="CalibratedNs">the time one task took on warmed code when the batch size was chosen; the batch holds
/// enough tasks that this estimate fills the sample target. The samples themselves can run faster than the estimate,
/// because code keeps getting optimised, so a sample can be shorter than the target by the same factor.</param>
public sealed record Timing(int ItersPerSample, double[] SamplesNs, WarmupResult? Warmup = null, double CalibratedNs = 0)
{
    public double Median => MedianSorted(SamplesNs);

    /// <summary>Median absolute deviation: the spread that one slow sample cannot inflate.</summary>
    public double Mad
    {
        get
        {
            var m = Median;
            var d = SamplesNs.Select(x => Math.Abs(x - m)).OrderBy(x => x).ToArray();
            return MedianSorted(d);
        }
    }

    public static double MedianSorted(double[] v)
    {
        if (v.Length == 0) return 0;
        int n = v.Length;
        return n % 2 == 1 ? v[n / 2] : (v[n / 2 - 1] + v[n / 2]) / 2;
    }
}

/// <summary>What one run used. <see cref="StateBytes"/> is the live bytes of the starting state alone.
/// <see cref="RetainedBytes"/> is what the run left live: the live heap one full collection after the run, less
/// the live heap just before it, with the starting state held in both readings.</summary>
public sealed record Usage(long PeakBytes, long AllocatedBytes, long StateBytes, long RetainedBytes);

/// <summary>The benchmark BenchmarkDotNet runs for every cell. The harness fills in the static fields
/// before each run, so one class serves all libraries, tasks and workloads.
///
/// BenchmarkDotNet calls <see cref="PrepareIteration"/> before each iteration, outside the clock, to build
/// the starting states; the single invocation of <see cref="Run"/> then performs the whole batch of
/// tasks, and the harness divides the time by the batch size. The states are released after the clock stops.</summary>
public class CellBenchmark
{
    public static Runner? Current;
    public static int BatchSize = 1;
    static Batch? _batch;

    [IterationSetup]
    public void PrepareIteration()
    {
        // BenchmarkDotNet collects garbage between iterations, which also empties the pools of reusable arrays
        // that libraries keep. One untracked task refills them, so the clock sees a program in its steady
        // state and not the first call after a collection.
        // A batch of one is a task long enough that refilling a pool is nothing next to it.
        if (BatchSize > 1) Current!.Prepare(1).Run();
        _batch = Current!.Prepare(BatchSize);
    }

    [Benchmark]
    public void Run() => _batch!.Run();

    [IterationCleanup]
    public void ReleaseIteration() => _batch = null;
}

public static class Measure
{
    static long Now() => Stopwatch.GetTimestamp();
    static double Ns(long ticks) => ticks * 1e9 / Stopwatch.Frequency;

    /// <summary>The bytes of live objects at a full, blocking, compacting collection: what the collector reports
    /// as promoted by it, so unreferenced objects and allocation slack are not in it. The figure is read
    /// before finalizers run. Library pools of reusable arrays are trimmed by a callback that runs on the
    /// finalizer thread after such a collection; reading first means a pool is counted in the same state
    /// every time, so a difference of two readings is the objects that were added between them.</summary>
    public static long LiveNow()
    {
        GC.Collect(2, GCCollectionMode.Forced, true, true);
        long live = GC.GetGCMemoryInfo(GCKind.FullBlocking).PromotedBytes;
        GC.WaitForPendingFinalizers();
        return live;
    }

    /// <summary>The live bytes one prepared state holds, measured on a throw-away state after an untracked
    /// run has filled whatever the library keeps for reuse.</summary>
    static long StateBytes(Runner r)
    {
        r.Prepare(1).Run();
        long before = LiveNow();
        var b = r.Prepare(1);
        long held = LiveNow() - before;
        GC.KeepAlive(b);
        return Math.Max(held, 1);
    }

    static ManualConfig BdnConfig(Config cfg) =>
        ManualConfig.CreateEmpty()
            .AddJob(Job.Default
                // The process is already running, so the cell is run inside it. The collector settings
                // here are the ones the project file gives the process (workstation, not concurrent).
                .WithToolchain(InProcessEmitToolchain.Instance)
                .WithStrategy(RunStrategy.Throughput)
                .WithGcServer(false).WithGcConcurrent(false)
                .WithLaunchCount(1)
                // The harness has already warmed the cell until the JIT stopped compiling; this one iteration
                // only takes BenchmarkDotNet through its own first call.
                .WithWarmupCount(1)
                .WithIterationCount(cfg.Samples)
                // One invocation per iteration, which runs the whole batch the set-up built.
                .WithInvocationCount(1).WithUnrollFactor(1)
                .WithEvaluateOverhead(false))
            .AddLogger(NullLogger.Instance)
            .WithOptions(ConfigOptions.DisableLogFile | ConfigOptions.DisableOptimizationsValidator)
            .WithArtifactsPath(ArtifactsPath);

    public static string ArtifactsPath = Path.Combine(Path.GetTempPath(), "oneyaml-bench-bdn");

    /// <summary>Runs the cell until the runtime has finished optimising what it calls: until the number of
    /// methods the JIT has compiled has not changed for <c>SettleWindowMs * SettleWindows</c>, at least
    /// <c>WarmupMinMs</c>, at most <c>WarmupMaxMs</c>. Each round is a batch of about 2 ms of work, so the
    /// calls that trigger the next tier come quickly.</summary>
    public static WarmupResult WarmUp(Runner r, Config cfg, long cap)
    {
        var clock = Stopwatch.StartNew();
        long first = JitInfo.GetCompiledMethodCount(false), seen = first;
        double lastChange = 0;
        int k = 1;
        while (true)
        {
            var b = r.Prepare(k);
            long t = Now();
            b.Run();
            double perTask = Ns(Now() - t) / k;
            k = (int)Math.Clamp(2e6 / Math.Max(perTask, 1), 1, Math.Min(cap, 1000));
            long c = JitInfo.GetCompiledMethodCount(false);
            double at = clock.Elapsed.TotalMilliseconds;
            if (c != seen) { seen = c; lastChange = at; }
            if (at >= cfg.WarmupMinMs && at - lastChange >= cfg.SettleWindowMs * cfg.SettleWindows)
                return new WarmupResult(at, true, seen - first);
            if (at >= cfg.WarmupMaxMs) return new WarmupResult(at, false, seen - first);
        }
    }

    /// <param name="stateBytes">the live bytes of one starting state, when the memory pass has already measured it</param>
    public static Timing Time(Runner r, Config cfg, long? stateBytes = null)
    {
        long cap = Math.Min(Math.Max(1, cfg.MaxPreparedBytes / Math.Max(stateBytes ?? StateBytes(r), 1)), cfg.MaxIters);
        var warm = WarmUp(r, cfg, cap);
        // Calibrate on single iterations of warmed code, keeping the fastest.
        double one = double.MaxValue, setup = double.MaxValue;
        for (int tries = 0; tries < 5; tries++)
        {
            long s = Now();
            var b = r.Prepare(1);
            setup = Math.Min(setup, Ns(Now() - s));
            long t = Now();
            b.Run();
            one = Math.Min(one, Ns(Now() - t));
        }
        // The batch always fills the sample target, whatever the starting states cost to build: a smaller batch
        // would make the fixed cost of one invocation part of the answer. A task that does not change its state
        // shares one (see Runner.SharedState), which keeps the set-up short; otherwise a cell whose states are
        // slow to build takes fewer samples, never fewer tasks per sample.
        int iters = (int)Math.Clamp(Math.Ceiling(cfg.SampleTargetMs * 1e6 / Math.Max(one, 1)), 1, cap);
        double perSampleMs = iters * ((r.SharedState ? 0 : setup) + one) / 1e6 + cfg.SampleTargetMs;
        int samples = (int)Math.Clamp(cfg.MaxCellMs / Math.Max(perSampleMs, 1e-3), cfg.MinSamples, cfg.Samples);

        CellBenchmark.Current = r;
        CellBenchmark.BatchSize = iters;
        var summary = BenchmarkRunner.Run(typeof(CellBenchmark), BdnConfig(cfg with { Samples = samples }));
        CellBenchmark.Current = null;
        var report = summary.Reports.SingleOrDefault();
        if (report == null || !report.Success)
            throw new InvalidOperationException("BenchmarkDotNet did not complete the run: " +
                string.Join("; ", summary.ValidationErrors.Select(e => e.Message)));
        var taken = report.AllMeasurements
            .Where(m => m.IterationMode == IterationMode.Workload && m.IterationStage == IterationStage.Actual)
            .Select(m => m.Nanoseconds / iters)
            .OrderBy(x => x)
            .ToArray();
        if (taken.Length == 0) throw new InvalidOperationException("BenchmarkDotNet reported no measurements");
        return new Timing(iters, taken, warm, one);
    }

    /// <summary>The bytes the task requests on this thread over one run of a throw-away state.</summary>
    static long Requested(Runner r)
    {
        var b = r.Prepare(1);
        long a0 = GC.GetAllocatedBytesForCurrentThread();
        b.Run();
        return GC.GetAllocatedBytesForCurrentThread() - a0;
    }

    /// <summary>One extra run of the task, taken apart from the timed ones so that tracking never slows
    /// them.
    ///
    /// Allocated: the bytes the task's own thread requested (<c>GC.GetAllocatedBytesForCurrentThread</c>),
    /// exact.
    ///
    /// Peak: the starting state plus every byte the task requested, as if nothing were collected. The run happens
    /// inside a no-GC region sized from an earlier run of the task, so no collection can happen during it, nothing
    /// is freed, and the heap grows by exactly what was requested. The collector's state before the run (what ran
    /// earlier, how full the young generation is) cannot change the figure, which is the same however the
    /// cells are ordered or filtered. If the region is too small and the runtime leaves it, the run is
    /// repeated with a larger one. The state is the live bytes after a full collection with the state built,
    /// minus those before it was built.
    ///
    /// Memory a library keeps for reuse between calls (pooled buffers, caches) is part of neither figure: an
    /// untracked run comes first, so it already exists. Retained is the part of it that one run adds.
    ///
    /// Retained: the live bytes one full collection after the run, less the live bytes just before the run, the
    /// starting state held in both readings and the result dropped inside the task. It is the same quantity as
    /// the Go harness reports, and shows what a run leaves in pools and caches that keep growing.</summary>
    public static Usage Memory(Runner r)
    {
        // One untracked run first, so the figures are the steady state of a program that has used the library
        // before: buffers a library keeps for reuse (array pools, caches) already exist and are not part of the
        // task or of its starting state.
        r.Prepare(1).Run();
        long h0 = LiveNow();
        var b = r.Prepare(1);
        // The garbage left by building the state is not part of the task; what survives is the state.
        long state = Math.Max(LiveNow() - h0, 0);
        // The collections above trimmed the library's pools; fill them again.
        long estimate = Requested(r);
        long allocated = -1;
        for (int attempt = 0; attempt < 4 && allocated < 0; attempt++)
        {
            long room = (estimate + (1L << 20)) << attempt;
            if (room > (8L << 30)) throw new InvalidOperationException("the task requests too much to measure in a no-GC region");
            // Room for the same number of bytes in the small and the large object heap, whichever the task uses.
            if (!GC.TryStartNoGCRegion(room * 2, room)) throw new InvalidOperationException("a no-GC region could not be started");
            long a0 = GC.GetAllocatedBytesForCurrentThread();
            b.Run();
            long a1 = GC.GetAllocatedBytesForCurrentThread();
            bool unbroken = GCSettings.LatencyMode == GCLatencyMode.NoGCRegion;
            if (unbroken) { GC.EndNoGCRegion(); allocated = a1 - a0; }
            else { b = r.Prepare(1); r.Prepare(1).Run(); }
        }
        if (allocated < 0) throw new InvalidOperationException("the task kept leaving the no-GC region");
        GC.KeepAlive(b);
        return new Usage(state + allocated, allocated, state, RetainedByOneRun(r));
    }

    /// <summary>The live bytes one run leaves behind: a full collection with a fresh starting state held, one run
    /// (the task drops its result), another full collection, and the difference. It is a pass of its own, after
    /// the peak and allocation figures are taken, because the collections it needs would trim what a library
    /// keeps in pools and change what the other passes see.</summary>
    static long RetainedByOneRun(Runner r)
    {
        var b = r.Prepare(1);
        long before = LiveNow();
        b.Run();
        long after = LiveNow();
        GC.KeepAlive(b);
        return Math.Max(after - before, 0);
    }
}
