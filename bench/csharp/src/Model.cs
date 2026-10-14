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
}

public abstract class Runner
{
    /// <summary>Runs the task once, as it is timed, and converts the result for the guard.</summary>
    public abstract Product Check();
    /// <summary>Builds <paramref name="n"/> starting states, outside the clock: independent ones, or one state
    /// that every run shares when <see cref="SharedState"/> is set.</summary>
    public abstract Batch Prepare(int n);
    /// <summary>For patch runners: applies the failing patch and reports the outcome.</summary>
    public virtual FailedPatch? CheckFailing() => null;
    /// <summary>True when the task only reads its starting state, so a batch may run all its tasks over one
    /// state instead of building one per task. A runner is believed only after its second answer equals a copy of
    /// its first and a used state costs what a fresh one does.</summary>
    public virtual bool SharedState => false;
    /// <summary>Why a runner that asked to share one state was given fresh states instead; null when it shares or
    /// did not ask. Not an error: the library's answers are right.</summary>
    public virtual string? SharedStateRefusal => null;
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
/// convert the result for the guard.
///
/// A runner that asks to share one starting state across a batch is not believed. Before it shares, two things
/// are checked, and a runner that fails either is run over fresh states instead (its answers are right, so the
/// cell is still measured):
/// the answer of a second run over a used state equals a copy of the first answer taken before that run (so a
/// buffer the task rewrites in place cannot pass by being compared with itself), and a run over a used state
/// allocates the same as a run over a fresh one (so a task that remembers its result, or that strips parts of its
/// state and so does less work later, cannot pass an answer-only check).</summary>
public sealed class FnRunner<TState, TOut>(
    Func<TState> setup, Func<TState, TOut> run, Func<TOut, Product> product,
    Func<FailedPatch>? failing = null, bool sharedState = false, string? variant = null) : Runner
{
    SharingVerdict? sharing;

    public override bool SharedState => sharedState && Sharing.Shares;
    public override string? SharedStateRefusal => sharedState && !Sharing.Shares ? Sharing.Reason : null;
    public override string? Variant => variant;

    SharingVerdict Sharing => sharing ??= VerifySharing();

    public override Product Check()
    {
        var state = setup();
        var first = product(run(state));
        if (sharedState) _ = Sharing;
        return first;
    }

    /// <summary>Allocation may differ by this much between a fresh and a used state: one part in a hundred and a
    /// few bytes for the boxes and strings a run makes by the way.</summary>
    public static bool CostsAgree(long fresh, long used) => Math.Abs(fresh - used) <= Math.Max(fresh, used) / 100 + 256;

    long Allocated(TState state)
    {
        long a0 = GC.GetAllocatedBytesForCurrentThread();
        var r = run(state);
        long a1 = GC.GetAllocatedBytesForCurrentThread();
        GC.KeepAlive(r);
        return a1 - a0;
    }

    SharingVerdict VerifySharing()
    {
        // Answers: the first is copied before the second run, because a task may hand back a buffer it owns.
        var state = setup();
        var firstCopy = Snapshot(product(run(state)));
        var second = Snapshot(product(run(state)));
        if (firstCopy != second)
            return new SharingVerdict(false, "the task changed its starting state: a second run over the same state gave another answer");

        // Cost: warm the code on throw-away states first, then compare a fresh state with the same state used again.
        // Taking the least of several readings of each keeps a one-off allocation (a pool filling, a first call)
        // from deciding the result, and cannot hide a task that is cheaper on a used state every time.
        var warm = setup();
        run(warm); run(warm);
        var clock = System.Diagnostics.Stopwatch.StartNew();
        long fresh = long.MaxValue, used = long.MaxValue;
        for (int round = 0; round < 3 && (round < 2 || clock.ElapsedMilliseconds < 500); round++)
        {
            var s = setup();
            fresh = Math.Min(fresh, Allocated(s));
            used = Math.Min(used, Allocated(s));
            Allocated(s); // a third and fourth use: a state that wears away does so over several runs
            used = Math.Min(used, Allocated(s));
        }
        if (!CostsAgree(fresh, used))
            return new SharingVerdict(false, $"a run over a used state allocates {used} bytes against {fresh} over a fresh one, so the task changes its starting state or remembers its result");
        return new SharingVerdict(true, "");
    }

    /// <summary>A copy of everything the guard would look at, taken now: bytes by value, trees as their text.</summary>
    static string Snapshot(Product p) => p switch
    {
        Product.Bytes x => "B:" + Convert.ToBase64String(x.Data),
        Product.Value x => "V:" + x.Tree.ToJson(),
        Product.Document x => "D:" + Convert.ToBase64String(x.Emitted) + ":" + (x.Tree == null ? "-" : x.Tree.ToJson()),
        _ => throw new InvalidOperationException("unknown product"),
    };

    sealed record SharingVerdict(bool Shares, string Reason);

    public override FailedPatch? CheckFailing() => failing?.Invoke();

    public override Batch Prepare(int n)
    {
        var states = new TState[n];
        if (SharedState)
        {
            var one = setup();
            Array.Fill(states, one);
        }
        else
            for (int i = 0; i < n; i++) states[i] = setup();
        return new FnBatch(states, run);
    }

    sealed class FnBatch(TState[] states, Func<TState, TOut> run) : Batch
    {
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
