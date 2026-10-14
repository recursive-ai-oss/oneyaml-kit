using System.Diagnostics;
using BenchmarkDotNet.Attributes;
using BenchmarkDotNet.Configs;
using BenchmarkDotNet.Engines;
using BenchmarkDotNet.Jobs;
using BenchmarkDotNet.Loggers;
using BenchmarkDotNet.Running;
using BenchmarkDotNet.Toolchains.InProcess.Emit;

namespace OneYamlKit.Bench;

/// <summary>How long to warm up and how many samples to take.</summary>
public sealed record Config(int WarmupMs, int Samples, int SampleTargetMs, long MaxPreparedBytes, int MaxIters)
{
    public static Config Quick => new(2, 5, 1, 64L << 20, 10_000);
    public static Config Full => new(300, 31, 50, 512L << 20, 1_000_000);

    /// <summary>Building the states of one sample may take this many times the sample target. A task much
    /// faster than the building of its state (writing back a parsed tree that holds its own text) would
    /// otherwise ask for thousands of states per sample and spend its whole run building them.</summary>
    public const int MaxSetupFactor = 20;

    /// <summary>Warm-up iterations handed to BenchmarkDotNet: enough to cover the warm-up time with
    /// iterations of about the sample length, and never fewer than one.</summary>
    public int WarmupIterations => Math.Max(1, (int)Math.Ceiling(WarmupMs / (double)Math.Max(1, SampleTargetMs)));
}

public sealed record Timing(int ItersPerSample, double[] SamplesNs)
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

/// <summary>What one run used. <see cref="StateBytes"/> is the live bytes of the starting state alone.</summary>
public sealed record Usage(long PeakBytes, long AllocatedBytes, long StateBytes);

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

    static void CollectAll()
    {
        GC.Collect(2, GCCollectionMode.Forced, true, true);
        GC.WaitForPendingFinalizers();
        GC.Collect(2, GCCollectionMode.Forced, true, true);
    }

    /// <summary>The bytes of live objects at a full, blocking, compacting collection: what the collector reports
    /// as promoted by it, so unreferenced objects and allocation slack are not in it. The figure is read
    /// before finalizers run. Library pools of reusable arrays are trimmed by a callback that runs on the
    /// finalizer thread after such a collection; reading first means a pool is counted in the same state
    /// every time, so a difference of two readings is the objects that were added between them.</summary>
    static long LiveNow()
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
                .WithWarmupCount(cfg.WarmupIterations)
                .WithIterationCount(cfg.Samples)
                // One invocation per iteration, which runs the whole batch the set-up built.
                .WithInvocationCount(1).WithUnrollFactor(1)
                .WithEvaluateOverhead(false))
            .AddLogger(NullLogger.Instance)
            .WithOptions(ConfigOptions.DisableLogFile | ConfigOptions.DisableOptimizationsValidator)
            .WithArtifactsPath(ArtifactsPath);

    public static string ArtifactsPath = Path.Combine(Path.GetTempPath(), "oneyaml-bench-bdn");

    /// <param name="stateBytes">the live bytes of one starting state, when the memory pass has already measured it</param>
    public static Timing Time(Runner r, Config cfg, long? stateBytes = null)
    {
        long cap = Math.Min(Math.Max(1, cfg.MaxPreparedBytes / Math.Max(stateBytes ?? StateBytes(r), 1)), cfg.MaxIters);
        // Calibrate on single iterations, keeping the fastest to ignore a cold start.
        double one = double.MaxValue, setup = double.MaxValue;
        long start = Now();
        for (int tries = 0; tries < 3 || (Ns(Now() - start) < cfg.WarmupMs * 1e6 && tries < 1000); tries++)
        {
            long s = Now();
            var b = r.Prepare(1);
            setup = Math.Min(setup, Ns(Now() - s));
            long t = Now();
            b.Run();
            one = Math.Min(one, Ns(Now() - t));
        }
        long setupCap = (long)Math.Max(1, Config.MaxSetupFactor * cfg.SampleTargetMs * 1e6 / Math.Max(setup, 1));
        int iters = (int)Math.Clamp(Math.Ceiling(cfg.SampleTargetMs * 1e6 / Math.Max(one, 1)), 1, Math.Min(cap, setupCap));

        CellBenchmark.Current = r;
        CellBenchmark.BatchSize = iters;
        var summary = BenchmarkRunner.Run(typeof(CellBenchmark), BdnConfig(cfg));
        CellBenchmark.Current = null;
        var report = summary.Reports.SingleOrDefault();
        if (report == null || !report.Success)
            throw new InvalidOperationException("BenchmarkDotNet did not complete the run: " +
                string.Join("; ", summary.ValidationErrors.Select(e => e.Message)));
        var samples = report.AllMeasurements
            .Where(m => m.IterationMode == IterationMode.Workload && m.IterationStage == IterationStage.Actual)
            .Select(m => m.Nanoseconds / iters)
            .OrderBy(x => x)
            .ToArray();
        if (samples.Length == 0) throw new InvalidOperationException("BenchmarkDotNet reported no measurements");
        return new Timing(iters, samples);
    }

    /// <summary>One extra run of the task, taken apart from the timed ones so that tracking never slows
    /// them.
    ///
    /// Allocated: the bytes the task's own thread requested (<c>GC.GetAllocatedBytesForCurrentThread</c>),
    /// exact.
    ///
    /// Memory a library keeps for reuse between calls (pooled buffers, caches) is part of neither figure: an
    /// untracked run comes first, so it already exists.
    ///
    /// Peak: the starting state plus the most the managed heap grew while the task ran. The state is the
    /// live bytes after a full collection with the state built, minus those before it was built. The growth is
    /// exact when no collection happened during the run, because nothing is freed then and the heap grew by
    /// exactly what the task requested. When a collection did happen, the growth is the highest heap size a
    /// second thread read (<c>GC.GetTotalMemory(false)</c>) or the size at the end, over the size at the
    /// start. Either way the size counts objects the task has dropped that the collector has not yet
    /// reclaimed, so it is the managed heap a process needs, not the live data alone.</summary>
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
        // The collections above trimmed the library's pools; fill them again, and start with an empty young
        // generation so a small task runs without a collection.
        r.Prepare(1).Run();
        GC.Collect(1, GCCollectionMode.Forced, true, false);

        long seen = 0;
        bool stop = false;
        var sampler = new Thread(() =>
        {
            long m = 0;
            while (!Volatile.Read(ref stop))
            {
                long v = GC.GetTotalMemory(false);
                if (v > m) m = v;
                Thread.Yield();
            }
            Volatile.Write(ref seen, m);
        })
        { IsBackground = true };
        sampler.Start();

        long hb = GC.GetTotalMemory(false);
        int gcs = GC.CollectionCount(0);
        long a0 = GC.GetAllocatedBytesForCurrentThread();
        b.Run();
        long a1 = GC.GetAllocatedBytesForCurrentThread();
        long end = GC.GetTotalMemory(false);
        bool collected = GC.CollectionCount(0) != gcs;
        Volatile.Write(ref stop, true);
        sampler.Join();
        GC.KeepAlive(b);
        long allocated = a1 - a0;
        long growth = collected ? Math.Max(Math.Max(Volatile.Read(ref seen), end) - hb, 0) : allocated;
        return new Usage(state + growth, allocated, state);
    }
}
