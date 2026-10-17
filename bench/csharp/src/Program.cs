using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using OneYamlKit.Bench.Libs;

namespace OneYamlKit.Bench;

/// <summary>In-process benchmark harness for the .NET YAML libraries.
///
/// <c>run</c> measures every (library, task, workload) cell that the workload set defines, after the
/// correctness guard has accepted what the library produced, and writes one JSON line per cell.
/// See docs/benchmarks.md and docs/benchmark-adapters.md for the protocol and the result format.</summary>
public static class Program
{
    internal sealed class Args
    {
        public string Cmd = "";
        public string Workloads = "workloads";
        public string? Out;
        public string Mode = "quick";
        /// <summary>Measure the cell in this process. The child processes of an isolated run are started with it.</summary>
        public bool SameProcess;
        public List<string> Libraries = [], Tasks = [], Only = [], Skip = [];
    }

    static Args ParseArgs(string[] argv)
    {
        if (argv.Length == 0) throw new ArgumentException("usage: OneYamlKit.Bench.CSharp <run|list> [options]");
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
                case "--same-process": a.SameProcess = true; break;
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

    static byte[] Line(Action<Utf8JsonWriter> write)
    {
        using var ms = new MemoryStream();
        using (var w = new Utf8JsonWriter(ms)) write(w);
        ms.WriteByte((byte)'\n');
        return ms.ToArray();
    }

    static void WriteLine(Stream s, Action<Utf8JsonWriter> write)
    {
        s.Write(Line(write));
        s.Flush();
    }

    static void HarnessRecord(Utf8JsonWriter w, Args a, List<ILibrary> libs)
    {
        w.WriteStartObject();
        w.WriteString("type", "harness");
        w.WriteString("name", "oneyaml-bench-csharp");
        w.WriteString("version", typeof(Program).Assembly.GetName().Version?.ToString(3) ?? "0");
        w.WriteString("language", "csharp");
        w.WriteString("runtime", RuntimeInformation.FrameworkDescription);
        w.WriteString("target", RuntimeInformation.RuntimeIdentifier);
        w.WriteString("mode", a.Mode);
        w.WriteString("tool", "BenchmarkDotNet " + Support.Version("BenchmarkDotNet"));
        // Said by what the run does, not by the mode alone: a child process of an isolated run is told to measure in place.
        string own = IsolatesCells(a)
            ? "; every cell is measured in a process of its own, because the heap that earlier cells leave behind changed the result. "
              + "Each process runs with " + ChildRuntimeSettings() + ", and every cell is first warmed up for "
              + Measure.WarmUpMilliseconds + " ms of whole batches before BenchmarkDotNet's own warm-up, because a new process starts at the runtime's unoptimised tier"
            : a.SameProcess
                ? "; all the cells of this run share one process"
                : "; the cells of a quick run share one process, so the run is a smoke result";
        var job = a.Mode == "quick" ? "dry job (one iteration)" : "ShortRun job (3 warm-up and 3 measured iterations)";
        w.WriteString("timer", "mean time per task from BenchmarkDotNet's " + job + " run in process; one invocation per iteration runs a batch of prepared tasks and the time is divided by the batch size" + own);
        w.WriteString("memory", "bytes allocated per task from BenchmarkDotNet's MemoryDiagnoser; allocation calls are not available");
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

    // ---- cells ---------------------------------------------------------------------------------

    internal sealed record Cell(string Status, string Detail, string? Variant = null, Measured? Measured = null);

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

    static Cell OneCell(ILibrary lib, BenchTask task, Workload w, bool quick)
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
        try
        {
            var m = Measure.Run(runner, w.Input.Length, quick);
            return new Cell("ok", "", runner.Variant ?? ok.Variant, m);
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
        if (c.Status == "ok" && c.Measured is { } m)
        {
            w.WriteString("variant", c.Variant);
            w.WriteStartObject("time");
            w.WriteNumber("ns", m.MeanNs);
            w.WriteString("stat", "mean");
            w.WriteNumber("median_ns", m.MedianNs);
            w.WriteNumber("batch", m.BatchSize);
            w.WriteEndObject();
            w.WriteStartObject("memory");
            w.WriteNumber("allocated_bytes", m.AllocatedBytes);
            w.WriteNull("allocations");
            w.WriteEndObject();
        }
        w.WriteEndObject();
    }

    /// <summary>Full mode, the one that is published, measures every cell in a process of its own. Quick mode is a
    /// smoke run: one process for all its cells keeps it to minutes, and its report says it is not a result.
    /// The child processes of an isolated run measure their cell in place.</summary>
    internal static bool IsolatesCells(Args a) => a.Mode == "full" && !a.SameProcess;

    /// <summary>What a child process reports for the one cell it ran.</summary>
    internal sealed record Spawned(int ExitCode, byte[]? CellLine);

    /// <summary>The result line of one cell, measured in a process of its own. Heap state left by earlier cells
    /// (their garbage, the memory they touched) changed the time of later ones, so no two cells share a process
    /// and a cell cannot be helped or hurt by the library that ran before it. A task the library cannot do
    /// needs no measurement and is answered here; any other cell, including one the guard will refuse, is
    /// handed to <paramref name="spawn"/>, which runs the whole cell (guard, then clock) in a fresh process.
    /// A process that dies is a cell with status error, as the driver records it.</summary>
    internal static byte[] IsolatedLine(ILibrary lib, BenchTask task, Workload w, Func<string, Spawned> spawn)
    {
        try { lib.Runner(task, w); }
        catch (Unsupported u) { return Line(x => WriteCell(x, lib, task, w, new Cell("unsupported", u.Message))); }
        catch (Exception) { /* the child reports it */ }
        var name = $"{lib.Id}/{task.Id()}/{w.Id}";
        var got = spawn(name);
        if (got is { ExitCode: 0, CellLine: { } line }) return line;
        var why = got.ExitCode == 0 ? "the process wrote no result" : $"the process died (exit status {got.ExitCode})";
        return Line(x => WriteCell(x, lib, task, w, new Cell("error", why)));
    }

    /// <summary>The runtime settings every child process runs with, as the run's timer text states them: the one this
    /// harness sets, and every DOTNET_ and COMPlus_ variable that was already in the environment (the children inherit
    /// them), each with its value, so that nothing a cell ran with goes unsaid.</summary>
    internal static string ChildRuntimeSettings()
    {
        var inherited = Environment.GetEnvironmentVariables().Keys.Cast<string>()
            .Where(k => k != "DOTNET_TC_CallCountingDelayMs" &&
                        (k.StartsWith("COMPlus_", StringComparison.OrdinalIgnoreCase) || k.StartsWith("DOTNET_", StringComparison.OrdinalIgnoreCase)))
            .Order(StringComparer.Ordinal).Select(k => $"{k}={Environment.GetEnvironmentVariable(k)}").ToList();
        return "DOTNET_TC_CallCountingDelayMs=0 (call counting starts immediately instead of after the default delay)" +
               (inherited.Count == 0 ? ", and no other DOTNET_ or COMPlus_ variable set" : ", and, inherited from the caller, " + string.Join(", ", inherited));
    }

    /// <summary>How a child process is started for one cell: this program again, told to measure that cell in place.</summary>
    internal static System.Diagnostics.ProcessStartInfo ChildStartInfo(Args a, string cell, string outFile)
    {
        var parts = cell.Split('/', 3);
        var psi = new System.Diagnostics.ProcessStartInfo(Environment.ProcessPath!) { UseShellExecute = false };
        // Run through the same host that started this process: a framework-dependent build is a dll, an apphost is not.
        var entry = System.Reflection.Assembly.GetEntryAssembly()!.Location;
        if (!string.Equals(Path.GetFileNameWithoutExtension(Environment.ProcessPath!), Path.GetFileNameWithoutExtension(entry), StringComparison.Ordinal))
            psi.ArgumentList.Add(entry);
        foreach (var x in new[] { "run", "--same-process", "--workloads", a.Workloads, "--mode", a.Mode, "--out", outFile,
                                 "--library", parts[0], "--task", parts[1], "--workload", parts[2] })
            psi.ArgumentList.Add(x);
        // A new process runs its code at the first, unoptimised tier, and the runtime waits 100 ms of quiet before it
        // starts promoting hot methods. The warm-up iterations of a small cell are shorter than that in total, so
        // without this the measured iterations would time unoptimised code (a 13 us task read 90 us). Starting the
        // promotion at once lets the warm-up do what it is for. Every library gets the same setting.
        // Set outright, not inherited, so the value a cell ran with is the one the run's timer text states.
        psi.Environment["DOTNET_TC_CallCountingDelayMs"] = "0";
        return psi;
    }

    static int _children;

    /// <summary>Runs one cell in a new process of this program and returns the cell line it wrote.</summary>
    static Spawned SpawnChild(Args a, string home, string cell)
    {
        var file = Path.Combine(home, $"dotnet-cell-{Environment.ProcessId}-{++_children}.jsonl");
        using var p = System.Diagnostics.Process.Start(ChildStartInfo(a, cell, file))!;
        p.WaitForExit();
        byte[]? line = null;
        try
        {
            foreach (var l in File.ReadAllLines(file))
                if (l.Contains("\"type\":\"cell\"", StringComparison.Ordinal)) line = Encoding.UTF8.GetBytes(l + "\n");
        }
        catch (IOException) { }
        try { File.Delete(file); } catch (IOException) { }
        return new Spawned(p.ExitCode, line);
    }

    static void Run(Args a)
    {
        bool quick = a.Mode switch { "quick" => true, "full" => false, _ => throw new ArgumentException($"unknown mode {a.Mode}") };
        var workloads = LoadWorkloads(a.Workloads, a.Only);
        var libs = AllLibraries().Where(l => a.Libraries.Count == 0 || a.Libraries.Contains(l.Id)).ToList();
        // BenchmarkDotNet wants a folder for its own files. It goes next to the output, so nothing lands in the
        // temporary directory, and it is removed when the run ends.
        var home = a.Out != null ? Path.GetDirectoryName(Path.GetFullPath(a.Out))! : Path.GetTempPath();
        Measure.ArtifactsPath = Path.Combine(home, $"dotnet-bdn-{Environment.ProcessId}");
        Stream outp = a.Out != null ? new FileStream(a.Out, FileMode.Create, FileAccess.Write, FileShare.Read) : Console.OpenStandardOutput();
        using (outp)
        {
            WriteLine(outp, w => HarnessRecord(w, a, libs));
            foreach (var wl in workloads)
                foreach (var task in wl.Tasks.Where(t => a.Tasks.Count == 0 || a.Tasks.Contains(t.Id())))
                    foreach (var lib in libs)
                    {
                        var name = $"{lib.Id}/{task.Id()}/{wl.Id}";
                        if (a.Skip.Contains(name)) continue;
                        // A crash cannot be caught, so mark the cell first; the driver reads the last mark
                        // to find the cell that killed the process.
                        WriteLine(outp, w => { w.WriteStartObject(); w.WriteString("type", "begin"); w.WriteString("cell", name); w.WriteEndObject(); });
                        if (!IsolatesCells(a))
                            WriteLine(outp, w => WriteCell(w, lib, task, wl, OneCell(lib, task, wl, quick)));
                        else
                        {
                            var line = IsolatedLine(lib, task, wl, n => SpawnChild(a, home, n));
                            outp.Write(line);
                            outp.Flush();
                        }
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
}
