using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using OneYamlKit.Bench.Libs;

namespace OneYamlKit.Bench;

/// <summary>In-process benchmark harness for the .NET YAML libraries.
///
/// <c>run</c> measures every (library, task, workload) cell that the workload set defines, after the
/// correctness guard has accepted what the library produced, and writes one JSON line per cell.
/// <c>once</c> runs one iteration of one cell. See docs/benchmarks.md and docs/benchmark-adapters.md for
/// the protocol and the result format.</summary>
public static class Program
{
    sealed class Args
    {
        public string Cmd = "";
        public string Workloads = "workloads";
        public string? Out;
        public string Mode = "quick";
        public bool GuardOnly => Mode == "guard";
        public List<string> Libraries = [], Tasks = [], Only = [], Skip = [];
    }

    static Args ParseArgs(string[] argv)
    {
        if (argv.Length == 0) throw new ArgumentException("usage: OneYamlKit.Bench.CSharp <run|once|list> [options]");
        var a = new Args { Cmd = argv[0] };
        for (int i = 1; i < argv.Length; i++)
        {
            string Val() => i + 1 < argv.Length ? argv[++i] : throw new ArgumentException($"{argv[i]} needs a value");
            switch (argv[i])
            {
                case "--workloads": a.Workloads = Val(); break;
                case "--out": a.Out = Val(); break;
                case "--mode": a.Mode = Val(); break;
                case "--library": a.Libraries.Add(Val()); break;
                case "--task": a.Tasks.Add(Val()); break;
                case "--workload": a.Only.Add(Val()); break;
                case "--skip": a.Skip.Add(Val()); break;
                default: throw new ArgumentException($"unknown option {argv[i]}");
            }
        }
        return a;
    }

    /// <summary>Every library, in the order the report lists them.</summary>
    public static List<ILibrary> AllLibraries()
    {
        var libs = new List<ILibrary>
        {
            new YamlDotNetLib(), new SharpYamlLib(), new SharpYamlModelLib(), new SharpYamlSyntaxLib(),
            new VYamlLib(), new Yaml2JsonNodeLib(),
        };
#if ONEYAML_LINKED
        libs.Add(new OneYamlReflectionLib());
        libs.Add(new OneYamlGeneratedLib());
#endif
#if SELFTEST
        foreach (var k in new[] { "correct", "wrong", "reject", "crash" }) libs.Add(new ControlLib(k));
#endif
        return libs;
    }

    static List<Workload> LoadWorkloads(string dir, List<string> only)
    {
        var index = J.Parse(File.ReadAllBytes(Path.Combine(dir, "index.json"))) as J.Obj
                    ?? throw new InvalidDataException("index.json is not an object");
        var list = new List<Workload>();
        if (index.V.TryGetValue("workloads", out var ws) && ws is J.Arr arr)
            foreach (var w in arr.V.OfType<J.Obj>())
            {
                var id = w.V["id"] is J.Str s ? s.V : "";
                if (only.Count > 0 && !only.Contains(id)) continue;
                list.Add(Workload.Load(Path.Combine(dir, id)));
            }
        return list;
    }

    public static int Main(string[] argv)
    {
        int code = 0;
        // The libraries recurse on nesting depth and so do the harness's own conversions; a very large stack
        // keeps the harness from being the one that runs out.
        var t = new Thread(() => code = Entry(argv), 512 * 1024 * 1024);
        t.Start();
        t.Join();
        return code;
    }

    static int Entry(string[] argv)
    {
        try
        {
            var a = ParseArgs(argv);
            switch (a.Cmd)
            {
                case "run": Run(a); break;
                case "once": Once(a); break;
                case "show": Show(a); break;
#if SELFTEST
                case "selftest": return SelfTests.Run();
#endif
                case "referee":
                    {
                        var text = Encoding.UTF8.GetString(File.ReadAllBytes(a.Workloads));
                        var x = Referee.Build(Referee.YamlDotNetEvents(text));
                        var y = Referee.Build(Referee.SharpYamlEvents(text));
                        Console.WriteLine("YamlDotNet: " + x.ToJson());
                        Console.WriteLine("SharpYaml:  " + y.ToJson());
                        Console.WriteLine("first difference: " + J.FirstDiff(x, y));
                        break;
                    }
                case "list":
                    foreach (var l in AllLibraries()) Console.WriteLine($"{l.Id} {l.Package} {l.Version} {RoleId(l.Role)}");
                    break;
                default: throw new ArgumentException($"unknown command {a.Cmd}");
            }
            return 0;
        }
        catch (Exception e) when (e is ArgumentException or IOException or InvalidDataException or JsonException)
        {
            Console.Error.WriteLine("OneYamlKit.Bench.CSharp: " + e.Message);
            return 2;
        }
    }

    static string RoleId(Role r) => r switch { Role.Own => "own", Role.Control => "control", _ => "competitor" };

    // ---- records -------------------------------------------------------------------------------

    static void WriteLine(Stream s, Action<Utf8JsonWriter> write)
    {
        using var ms = new MemoryStream();
        using (var w = new Utf8JsonWriter(ms)) write(w);
        ms.WriteByte((byte)'\n');
        ms.Position = 0;
        ms.CopyTo(s);
        s.Flush();
    }

    static void HarnessRecord(Utf8JsonWriter w, Args a, Config cfg, List<ILibrary> libs)
    {
        w.WriteStartObject();
        w.WriteString("type", "harness");
        w.WriteString("name", "oneyaml-bench-csharp");
        w.WriteString("version", typeof(Program).Assembly.GetName().Version?.ToString(3) ?? "0");
        w.WriteString("language", "csharp");
        w.WriteString("runtime", RuntimeInformation.FrameworkDescription);
        w.WriteString("target", RuntimeInformation.RuntimeIdentifier);
        w.WriteStartObject("flags");
        w.WriteString("configuration", "Release");
        w.WriteString("benchmarkdotnet", Support.Version("BenchmarkDotNet"));
        w.WriteString("job", "InProcessEmitToolchain, Throughput, InvocationCount=1, UnrollFactor=1, WithGcServer(false), WithGcConcurrent(false), EvaluateOverhead=false");
        w.WriteString("gc", GCSettingsText());
        // The runtime runs with its defaults, as a user's program does; the warm-up (see Measure.WarmUp) brings
        // each cell to optimised, profiled code before it is timed. A run that was started with one of these
        // switched off says so here.
        w.WriteString("tiered_compilation", Switch("System.Runtime.TieredCompilation", "TieredCompilation"));
        w.WriteString("quick_jit", Switch("System.Runtime.TieredCompilation.QuickJit", "TC_QuickJit"));
        w.WriteString("tiered_pgo", Switch("System.Runtime.TieredPGO", "TieredPGO"));
        w.WriteString("warmup", $"each cell runs until the number of methods the JIT has compiled stays unchanged for {cfg.SettleWindowMs * cfg.SettleWindows} ms (at least {cfg.WarmupMinMs} ms, at most {cfg.WarmupMaxMs} ms), so the code that is timed is the optimised code that dynamic PGO produces; the cell records how long it took and whether it settled");
        w.WriteEndObject();
        w.WriteString("mode", a.Mode);
        w.WriteStartObject("config");
        w.WriteNumber("warmup_ms", cfg.WarmupMinMs);
        w.WriteNumber("samples", cfg.Samples);
        w.WriteNumber("sample_target_ms", cfg.SampleTargetMs);
        w.WriteNumber("settle_window_ms", cfg.SettleWindowMs);
        w.WriteNumber("settle_windows", cfg.SettleWindows);
        w.WriteNumber("warmup_max_ms", cfg.WarmupMaxMs);
        w.WriteNumber("min_samples", cfg.MinSamples);
        w.WriteEndObject();
        w.WriteString("timer", "BenchmarkDotNet, one invocation per iteration that runs a batch of prepared tasks (IterationSetup builds the states, IterationCleanup releases them); time per task = invocation time / batch size; median and median absolute deviation of the iterations; the batch always fills the sample target, and a cell whose states are slow to build takes fewer samples (never below min_samples)");
        w.WriteString("memory", "managed heap: allocated is the bytes requested by the task (GC.GetAllocatedBytesForCurrentThread, exact); peak is the starting state (live bytes after a full collection) plus the bytes requested, measured in a no-GC region so no collection frees anything during the run and the figure does not depend on the collector's earlier state; retained is the live bytes one full collection after a run, less the live bytes just before it, with the starting state held (what the library keeps in pools and caches); allocation calls are not available");
        w.WriteBoolean("selftest_build",
#if SELFTEST
            true
#else
            false
#endif
        );
        w.WriteStartArray("libraries");
        foreach (var l in libs)
        {
            w.WriteStartObject();
            w.WriteString("id", l.Id);
            w.WriteString("package", l.Package);
            w.WriteString("version", l.Version);
            w.WriteString("role", RoleId(l.Role));
            w.WriteStartObject("settings");
            foreach (var (k, v) in l.Settings) w.WriteString(k, v);
            w.WriteEndObject();
            w.WriteEndObject();
        }
        w.WriteEndArray();
        w.WriteEndObject();
    }

    static string Switch(string name, string env)
    {
        var e = Environment.GetEnvironmentVariable("DOTNET_" + env) ?? Environment.GetEnvironmentVariable("COMPlus_" + env);
        if (e != null) return e == "0" ? "off (environment)" : "on (environment)";
        return AppContext.TryGetSwitch(name, out var on) ? (on ? "on" : "off") : "default (on)";
    }

    static string GCSettingsText()
    {
        var s = System.Runtime.GCSettings.IsServerGC ? "server" : "workstation";
        return s + (AppContext.TryGetSwitch("System.GC.Concurrent", out var c) && c ? ", concurrent" : ", non-concurrent");
    }

    // ---- cells ---------------------------------------------------------------------------------

    sealed record Cell(string Status, string Detail, string? Variant = null, Timing? Time = null, Usage? Memory = null);

    static readonly System.Text.RegularExpressions.Regex LeadingPosition = new(@"^\(Lin: \d+, Col: \d+, Chr: \d+\)( - \(Lin: \d+, Col: \d+, Chr: \d+\))?: ");

    static string Describe(Exception e)
    {
        var inner = e is System.Reflection.TargetInvocationException { InnerException: { } i } ? i : e;
        // SharpYaml starts its messages with the position twice, which the cell has no use for.
        var message = LeadingPosition.Replace(inner.Message, "");
        var text = $"{inner.GetType().Name}: {message}".Replace('\n', ' ').Replace('\r', ' ');
        // The depth limits are defaults a user gets and the harness never raises; saying so up front keeps it in the
        // cell's detail, which the report shows, even when the message is clipped.
        if (inner.GetType().Name == "MaximumRecursionLevelReachedException" || message.Contains("maximum nesting depth", StringComparison.OrdinalIgnoreCase))
            text = "default limit, not raised: " + text;
        return J.Clip(text, 160);
    }

    static Cell OneCell(ILibrary lib, BenchTask task, Workload w, Config cfg, Args a)
    {
        Runner runner;
        try { runner = lib.Runner(task, w); }
        catch (Unsupported u) { return new Cell("unsupported", u.Message); }
        catch (Exception e) { return new Cell("error", "while preparing: " + Describe(e)); }

        // Guard first: run the task once and judge the answer. A wrong answer gets no number.
        Verdict verdict;
        try
        {
            verdict = Guard.Judge(task, w,
                () =>
                {
                    try { return runner.Check(); }
                    catch (Rejected) { throw; }
                    // An exception from the library is a refusal of the input, as a panic is in Rust.
                    catch (Exception e) when (e is not OutOfMemoryException) { throw new Rejected(Describe(e)); }
                },
                () => runner.CheckFailing(),
                Referee.Read);
        }
        catch (Exception e) { return new Cell("error", "while judging: " + Describe(e)); }

        if (verdict is not Verdict.Ok ok) return new Cell(verdict.Status, verdict.Detail);
        // Only the verdicts are wanted when the known deviations are being recorded.
        if (a.GuardOnly) return new Cell("ok", "", runner.Variant ?? ok.Variant);
        try
        {
            // Time first, which warms the cell: the memory pass then sees the optimised code a user's program runs.
            // Unoptimised code allocates more (it boxes and copies what the optimiser keeps on the stack), so a
            // figure taken before the warm-up would depend on how much the cell had already run.
            var t = Measure.Time(runner, cfg);
            var m = Measure.Memory(runner);
            return new Cell("ok", "", runner.Variant ?? ok.Variant, t, m);
        }
        catch (Exception e) { return new Cell("error", "while measuring: " + Describe(e)); }
    }

    static void WriteCell(Utf8JsonWriter w, ILibrary lib, BenchTask task, Workload w2, Cell c)
    {
        w.WriteStartObject();
        w.WriteString("type", "cell");
        w.WriteString("library", lib.Id);
        w.WriteString("task", task.Id());
        w.WriteString("workload", w2.Id);
        w.WriteNumber("input_bytes", w2.Input.Length);
        w.WriteString("status", c.Status);
        w.WriteString("detail", c.Detail);
        if (c.Status == "ok" && c.Time == null) w.WriteString("variant", c.Variant);
        if (c.Status == "ok" && c.Time != null && c.Memory != null)
        {
            w.WriteString("variant", c.Variant);
            w.WriteStartObject("time");
            w.WriteNumber("iters_per_sample", c.Time.ItersPerSample);
            w.WriteNumber("samples", c.Time.SamplesNs.Length);
            w.WriteNumber("median_ns", c.Time.Median);
            w.WriteNumber("mad_ns", c.Time.Mad);
            w.WriteNumber("min_ns", c.Time.SamplesNs[0]);
            w.WriteNumber("max_ns", c.Time.SamplesNs[^1]);
            if (c.Time.Warmup is { } wu)
            {
                w.WriteNumber("warmup_ms", Math.Round(wu.Ms, 1));
                w.WriteBoolean("warmup_settled", wu.Settled);
                w.WriteNumber("warmup_compiled_methods", wu.Compiled);
            }
            w.WriteStartArray("samples_ns");
            foreach (var x in c.Time.SamplesNs) w.WriteNumberValue(x);
            w.WriteEndArray();
            w.WriteEndObject();
            w.WriteStartObject("memory");
            w.WriteString("method", MemoryMethod);
            w.WriteNumber("peak_bytes", c.Memory.PeakBytes);
            w.WriteNumber("allocated_bytes", c.Memory.AllocatedBytes);
            w.WriteNull("allocations");
            w.WriteNumber("retained_bytes", c.Memory.RetainedBytes);
            w.WriteEndObject();
        }
        w.WriteEndObject();
    }

    /// <summary>The name a cell's memory figures carry, so a table never mixes this method with another.</summary>
    public const string MemoryMethod = "GC.GetAllocatedBytesForCurrentThread, no-GC region";

    static Config ConfigFor(string mode) => mode switch
    {
        "quick" or "guard" => Config.Quick,
        "full" => Config.Full,
        _ => throw new ArgumentException($"unknown mode {mode}"),
    };

    static void Run(Args a)
    {
        var cfg = ConfigFor(a.Mode);
        var workloads = LoadWorkloads(a.Workloads, a.Only);
        var libs = AllLibraries().Where(l => a.Libraries.Count == 0 || a.Libraries.Contains(l.Id)).ToList();
        // BenchmarkDotNet wants a folder for its own files. It goes next to the output, so nothing lands in the
        // temporary directory, and it is removed when the run ends.
        var home = a.Out != null ? Path.GetDirectoryName(Path.GetFullPath(a.Out))! : Path.GetTempPath();
        Measure.ArtifactsPath = Path.Combine(home, $"dotnet-bdn-{Environment.ProcessId}");
        Stream outp = a.Out != null ? new FileStream(a.Out, FileMode.Create, FileAccess.Write, FileShare.Read) : Console.OpenStandardOutput();
        using (outp)
        {
            WriteLine(outp, w => HarnessRecord(w, a, cfg, libs));
            foreach (var wl in workloads)
                foreach (var task in wl.Tasks.Where(t => a.Tasks.Count == 0 || a.Tasks.Contains(t.Id())))
                    foreach (var lib in libs)
                    {
                        var name = $"{lib.Id}/{task.Id()}/{wl.Id}";
                        if (a.Skip.Contains(name)) continue;
                        // A crash cannot be caught, so mark the cell first; the driver reads the last mark
                        // to find the cell that killed the process.
                        WriteLine(outp, w => { w.WriteStartObject(); w.WriteString("type", "begin"); w.WriteString("cell", name); w.WriteEndObject(); });
                        var cell = OneCell(lib, task, wl, cfg, a);
                        WriteLine(outp, w => WriteCell(w, lib, task, wl, cell));
                    }
        }
        try { Directory.Delete(Measure.ArtifactsPath, true); } catch (IOException) { } catch (UnauthorizedAccessException) { }
    }

    /// <summary>Prints what a library produced for one cell, for a person looking into a verdict: the bytes
    /// as they are, or the value in the common form.</summary>
    static void Show(Args a)
    {
        var task = TaskIds.Parse(a.Tasks.FirstOrDefault() ?? "") ?? throw new ArgumentException("unknown task");
        var w = LoadWorkloads(a.Workloads, [a.Only.FirstOrDefault() ?? ""]).SingleOrDefault() ?? throw new ArgumentException("unknown workload");
        var lib = AllLibraries().FirstOrDefault(l => l.Id == a.Libraries.FirstOrDefault()) ?? throw new ArgumentException("unknown library");
        switch (lib.Runner(task, w).Check())
        {
            case Product.Bytes b: Console.Out.Write(Encoding.UTF8.GetString(b.Data)); break;
            case Product.Value v: Console.WriteLine(v.Tree); break;
            case Product.Document d: Console.WriteLine(d.Tree?.ToString()); Console.Out.Write(Encoding.UTF8.GetString(d.Emitted)); break;
        }
    }

    /// <summary>Runs one cell once: no guard, no clock.</summary>
    static void Once(Args a)
    {
        if (a.Libraries.Count == 0 || a.Tasks.Count == 0 || a.Only.Count == 0)
            throw new ArgumentException("once needs --library, --task and --workload");
        var task = TaskIds.Parse(a.Tasks[0]) ?? throw new ArgumentException("unknown task");
        var w = LoadWorkloads(a.Workloads, [a.Only[0]]).SingleOrDefault() ?? throw new ArgumentException("unknown workload");
        var lib = AllLibraries().FirstOrDefault(l => l.Id == a.Libraries[0]) ?? throw new ArgumentException("unknown library");
        lib.Runner(task, w).Prepare(1).Run();
    }
}
