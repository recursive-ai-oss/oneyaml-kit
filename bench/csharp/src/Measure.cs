using BenchmarkDotNet.Attributes;
using BenchmarkDotNet.Configs;
using BenchmarkDotNet.Diagnosers;
using BenchmarkDotNet.Jobs;
using BenchmarkDotNet.Loggers;
using BenchmarkDotNet.Running;
using BenchmarkDotNet.Toolchains.InProcess.Emit;

namespace OneYamlKit.Bench;

/// <summary>What BenchmarkDotNet reports for a cell, per task: mean and median time, and the bytes the task
/// allocated (MemoryDiagnoser).</summary>
public sealed record Measured(double MeanNs, double MedianNs, long AllocatedBytes, int BatchSize);

/// <summary>The benchmark BenchmarkDotNet runs for every cell. The harness fills in the static fields
/// before each run, so one class serves all libraries, tasks and workloads.
///
/// BenchmarkDotNet calls <see cref="PrepareIteration"/> before each iteration, outside the clock, to build
/// the starting states; the single invocation of <see cref="Run"/> then performs the whole batch of
/// tasks, and the harness divides by the batch size. The states are released after the clock stops.</summary>
public class CellBenchmark
{
    public static Runner? Current;
    public static int BatchSize = 1;
    static Batch? _batch;

    [IterationSetup]
    public void PrepareIteration() => _batch = Current!.Prepare(BatchSize);

    [Benchmark]
    public void Run() => _batch!.Run();

    [IterationCleanup]
    public void ReleaseIteration() => _batch = null;
}

public static class Measure
{
    /// <summary>A scratch folder for BenchmarkDotNet's own files; the harness removes it when the run ends.</summary>
    public static string ArtifactsPath = Path.Combine(Path.GetTempPath(), "oneyaml-bench-bdn");

    /// <summary>States built for one invocation: enough that the invocation is not a handful of microseconds,
    /// few enough that a large input does not fill the heap with starting states.</summary>
    public static int BatchFor(int inputBytes) => Math.Clamp(4_000_000 / Math.Max(inputBytes, 1), 1, 1000);

    static ManualConfig Config(bool quick)
    {
        // Quick mode runs BenchmarkDotNet's own dry job (one iteration); full mode runs its ShortRun job (3
        // warm-up and 3 measured iterations), the documented setting for work that takes long per invocation.
        // Its default job keeps iterating until the spread settles, which takes minutes on a large input.
        // Both run in this process, because the harness already holds the libraries and the workloads, and
        // with one invocation per iteration because each invocation consumes the starting states that
        // IterationSetup built.
        var job = (quick ? Job.Dry : Job.ShortRun)
            .WithToolchain(InProcessEmitToolchain.Instance)
            .WithInvocationCount(1).WithUnrollFactor(1);
        return ManualConfig.CreateEmpty()
            .AddJob(job)
            .AddDiagnoser(MemoryDiagnoser.Default)
            .AddLogger(NullLogger.Instance)
            .WithOptions(ConfigOptions.DisableLogFile | ConfigOptions.DisableOptimizationsValidator)
            .WithArtifactsPath(ArtifactsPath);
    }

    public static Measured Run(Runner r, int inputBytes, bool quick)
    {
        int batch = BatchFor(inputBytes);
        CellBenchmark.Current = r;
        CellBenchmark.BatchSize = batch;
        var summary = BenchmarkRunner.Run(typeof(CellBenchmark), Config(quick));
        CellBenchmark.Current = null;
        var report = summary.Reports.SingleOrDefault();
        if (report == null || !report.Success || report.ResultStatistics == null || report.GcStats.GetBytesAllocatedPerOperation(report.BenchmarkCase) is not { } bytes)
            throw new InvalidOperationException("BenchmarkDotNet did not complete the run: " +
                string.Join("; ", summary.ValidationErrors.Select(e => e.Message)));
        var s = report.ResultStatistics;
        return new Measured(s.Mean / batch, s.Median / batch, bytes / batch, batch);
    }
}
