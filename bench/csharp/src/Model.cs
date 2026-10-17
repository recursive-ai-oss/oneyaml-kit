using System.Text;

namespace OneYamlKit.Bench;

/// <summary>The seven tasks. Each is defined in docs/benchmarks.md; the harness times exactly the
/// code the correctness guard checked.</summary>
public enum BenchTask { TypedLoad, GenericLoad, Dump, DocumentParse, UnchangedEmit, EditScript, Patch }

public static class TaskIds
{
    public static readonly BenchTask[] All = Enum.GetValues<BenchTask>();

    public static string Id(this BenchTask t) => t switch
    {
        BenchTask.TypedLoad => "typed-load",
        BenchTask.GenericLoad => "generic-load",
        BenchTask.Dump => "dump",
        BenchTask.DocumentParse => "document-parse",
        BenchTask.UnchangedEmit => "unchanged-emit",
        BenchTask.EditScript => "edit-script",
        _ => "patch",
    };

    public static BenchTask? Parse(string s) => All.Select(t => (BenchTask?)t).FirstOrDefault(t => t!.Value.Id() == s);
}

/// <summary>One edit or patch step, as in RFC 6902.</summary>
public sealed record Op(string Name, string Path, J? Value);

/// <summary>A workload as the guard and the libraries see it.</summary>
public sealed class Workload
{
    public required string Id { get; init; }
    public required byte[] Input { get; init; }
    public required string InputText { get; init; }
    public int Documents { get; init; } = 1;
    public List<BenchTask> Tasks { get; init; } = [];
    public string? TypedModel { get; init; }
    /// <summary>Expected values by variant name: "spec" first, then e.g. "merged".</summary>
    public List<(string Name, J Value)> Expected { get; init; } = [];
    public List<Op> EditOps { get; init; } = [];
    public byte[]? EditExpected { get; init; }
    public List<Op> PatchOps { get; init; } = [];
    public List<Op> PatchFailingOps { get; init; } = [];
    public byte[]? PatchExpected { get; init; }

    public J BaseValue => Expected[0].Value;

    /// <summary>A fresh copy of the input bytes. Every prepared state takes its own, in setup and outside
    /// the clock, so no state reads memory that another state, or the harness, has just touched.</summary>
    public byte[] OwnBytes() => (byte[])Input.Clone();

    /// <summary>A fresh copy of the input as a string object of its own (never the shared instance).</summary>
    public string OwnText() => InputText.Length == 0 ? InputText : new string(InputText.AsSpan());

    static readonly UTF8Encoding Strict = new(false, true);

    public static Workload Load(string dir)
    {
        var meta = J.Parse(File.ReadAllBytes(Path.Combine(dir, "workload.json"))) as J.Obj
                   ?? throw new InvalidDataException("workload.json is not an object");
        var input = File.ReadAllBytes(Path.Combine(dir, "input.yaml"));
        string text;
        try { text = Strict.GetString(input); }
        catch (Exception e) { throw new InvalidDataException($"{dir}: input is not UTF-8: {e.Message}"); }
        var tasks = new List<BenchTask>();
        if (meta.V.TryGetValue("tasks", out var ts) && ts is J.Arr ta)
            foreach (var t in ta.V)
                if (t is J.Str s && TaskIds.Parse(s.V) is BenchTask parsed) tasks.Add(parsed);
        var expected = new List<(string, J)> { ("spec", J.Parse(File.ReadAllBytes(Path.Combine(dir, "expected.json")))) };
        var merged = Path.Combine(dir, "expected.merged.json");
        if (File.Exists(merged)) expected.Add(("merged", J.Parse(File.ReadAllBytes(merged))));

        List<Op> Ops(string file, string key)
        {
            var p = Path.Combine(dir, file);
            if (!File.Exists(p)) return [];
            var v = J.Parse(File.ReadAllBytes(p)) as J.Obj;
            var list = new List<Op>();
            if (v != null && v.V.TryGetValue(key, out var arr) && arr is J.Arr a)
                foreach (var o in a.V.OfType<J.Obj>())
                    list.Add(new Op(Str(o, "op"), Str(o, "path"), o.V.GetValueOrDefault("value")));
            return list;
        }

        byte[]? Bytes(string file) => File.Exists(Path.Combine(dir, file)) ? File.ReadAllBytes(Path.Combine(dir, file)) : null;

        return new Workload
        {
            Id = Str(meta, "id"),
            Input = input,
            InputText = text,
            Documents = meta.V.TryGetValue("documents", out var d) && d is J.Int di ? (int)di.V : 1,
            Tasks = tasks,
            TypedModel = meta.V.TryGetValue("typed_model", out var tm) && tm is J.Str tms ? tms.V : null,
            Expected = expected,
            EditOps = Ops("edit.json", "ops"),
            EditExpected = Bytes("edit.expected.yaml"),
            PatchOps = Ops("patch.json", "ops"),
            PatchFailingOps = Ops("patch.json", "failing_ops"),
            PatchExpected = Bytes("patch.expected.yaml"),
        };
    }

    static string Str(J.Obj o, string k) => o.V.TryGetValue(k, out var v) && v is J.Str s ? s.V : "";
}

/// <summary>What a library produced for the guard to judge.</summary>
public abstract record Product
{
    /// <summary>A tree in the common form (a list of documents when there are several).</summary>
    public sealed record Value(J Tree) : Product;
    public sealed record Bytes(byte[] Data) : Product;
    /// <summary>A parsed document: its value (null for a library with no way to read it back as data,
    /// which is then judged on its bytes alone) and what it writes back unchanged.</summary>
    public sealed record Document(J? Tree, byte[] Emitted) : Product;
}

/// <summary>What a library did with the failing patch (the one with a step that cannot apply).</summary>
public sealed record FailedPatch(bool Errored, byte[] Bytes);

/// <summary>The library refused the input or the request. The message is shown in the cell.</summary>
public sealed class Rejected(string message) : Exception(message);

/// <summary>Why a library has no runner for a task.</summary>
public sealed class Unsupported(string reason) : Exception(reason);

public enum Role { Own, Competitor, Control }

/// <summary>Prepared iterations of a task. <see cref="Run"/> performs all of them and drops their
/// results; it is the only code the clock sees.</summary>
public abstract class Batch
{
    public abstract void Run();
    /// <summary>The prepared states, so a self-test can check that no two of them share an object.</summary>
    public abstract IReadOnlyList<object?> States { get; }
}

public abstract class Runner
{
    /// <summary>Runs the task once, as it is timed, and converts the result for the guard.</summary>
    public abstract Product Check();
    /// <summary>Builds <paramref name="n"/> independent starting states, outside the clock.</summary>
    public abstract Batch Prepare(int n);
    /// <summary>For patch runners: applies the failing patch and reports the outcome.</summary>
    public virtual FailedPatch? CheckFailing() => null;
    /// <summary>The variant a correct answer is reported as, when it is not the guard's (for example
    /// <c>harness-rendered</c>); null keeps the guard's.</summary>
    public virtual string? Variant => null;
}

public interface ILibrary
{
    string Id { get; }
    string Package { get; }
    string Version { get; }
    Role Role { get; }
    IReadOnlyDictionary<string, string> Settings { get; }
    /// <summary>A runner for the task, or throws <see cref="Unsupported"/>.</summary>
    Runner Runner(BenchTask task, Workload w);
}

/// <summary>A runner built from three functions: make a starting state, do the timed work on it, and
/// convert the result for the guard.</summary>
public sealed class FnRunner<TState, TOut>(
    Func<TState> setup, Func<TState, TOut> run, Func<TOut, Product> product,
    Func<FailedPatch>? failing = null, string? variant = null) : Runner
{
    public override string? Variant => variant;

    public override Product Check() => product(run(setup()));

    public override FailedPatch? CheckFailing() => failing?.Invoke();

    public override Batch Prepare(int n)
    {
        var states = new TState[n];
        for (int i = 0; i < n; i++) states[i] = setup();
        return new FnBatch(states, run);
    }

    sealed class FnBatch(TState[] states, Func<TState, TOut> run) : Batch
    {
        public override IReadOnlyList<object?> States => states.Select(s => (object?)s).ToList();

        public override void Run()
        {
            var s = states;
            var f = run;
            for (int i = 0; i < s.Length; i++)
            {
                // The result goes out of scope here: dropping it is part of the task, as it is for a caller.
                _ = f(s[i]);
            }
        }
    }
}

public static class Pointer
{
    /// <summary>A step of a path into a document.</summary>
    public readonly record struct Seg(string? Key, int Index)
    {
        public bool IsKey => Key != null;
    }

    public static List<string> Tokens(string ptr) =>
        ptr.Length == 0 ? [] : ptr[1..].Split('/').Select(t => t.Replace("~1", "/").Replace("~0", "~")).ToList();

    /// <summary>Resolves a pointer against the workload's value: a token is an index where the value
    /// there is a sequence and a key where it is a mapping. <paramref name="lastNew"/> allows the final
    /// token to name something that does not exist yet (an add).</summary>
    public static List<Seg> Segments(J baseValue, string ptr, bool lastNew)
    {
        var toks = Tokens(ptr);
        var cur = baseValue;
        var outp = new List<Seg>();
        for (int i = 0; i < toks.Count; i++)
        {
            var t = toks[i];
            bool last = i + 1 == toks.Count;
            switch (cur)
            {
                case J.Arr a:
                    if (t == "-" && last && lastNew) throw new InvalidOperationException("append");
                    if (!int.TryParse(t, out var n) || n < 0) throw new InvalidOperationException($"bad index \"{t}\" in {ptr}");
                    outp.Add(new Seg(null, n));
                    cur = n < a.V.Count ? a.V[n] : throw new InvalidOperationException($"no element {n} in {ptr}");
                    break;
                case J.Obj o:
                    outp.Add(new Seg(t, 0));
                    if (o.V.TryGetValue(t, out var next)) cur = next;
                    else if (last && lastNew) return outp;
                    else throw new InvalidOperationException($"no key \"{t}\" in {ptr}");
                    break;
                default:
                    throw new InvalidOperationException($"cannot descend into a scalar at \"{t}\" in {ptr}");
            }
        }
        return outp;
    }
}
