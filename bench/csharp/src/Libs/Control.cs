#if SELFTEST
using System.Text;

namespace OneYamlKit.Bench.Libs;

/// <summary>Controls: fake libraries that prove the guard bites. They exist only in the self-test build
/// (a different assembly), so a measurement build can never include one.
///
/// <c>Correct</c> returns the expectation itself (the guard must accept it), <c>Wrong</c> returns the
/// expectation with one small change (the guard must refuse it and no time may be recorded), <c>Reject</c>
/// refuses every input, and <c>Crash</c> ends the process on a generic load.</summary>
public sealed class ControlLib(string kind) : ILibrary
{
    public string Id => "control-" + kind;
    public string Package => "control";
    public string Version => "0";
    public Role Role => Role.Control;
    public IReadOnlyDictionary<string, string> Settings { get; } = new Dictionary<string, string>();

    /// <summary>Changes the last leaf of a value, so a comparison that looks at too little misses it.</summary>
    static J Perturb(J v) => v switch
    {
        J.Str s => new J.Str(s.V + "!"),
        J.Int i => new J.Float((double)i.V + 1),
        J.Float f => new J.Float(f.V + 1),
        J.Bool b => new J.Bool(!b.V),
        J.Null => new J.Bool(false),
        J.Arr a when a.V.Count > 0 => new J.Arr([.. a.V.Take(a.V.Count - 1), Perturb(a.V[^1])]),
        J.Obj o when o.V.Count > 0 => PerturbObj(o),
        _ => v,
    };

    static J PerturbObj(J.Obj o)
    {
        var r = new J.Obj();
        var last = o.V.Keys.Last();
        foreach (var (k, x) in o.V) r.Put(k, k == last ? Perturb(x) : x);
        return r;
    }

    byte[] Tweak(byte[] b) => kind == "wrong" ? [.. b, (byte)' '] : b.ToArray();

    public Runner Runner(BenchTask task, Workload w)
    {
        if (kind == "crash" && task == BenchTask.GenericLoad)
            return new FnRunner<int, int>(() => 0, _ => { Environment.FailFast("control-crash"); return 0; },
                _ => new Product.Bytes([]));
        J Value() => kind == "wrong" ? Perturb(w.BaseValue) : w.BaseValue;
        void Fail() { if (kind == "reject") throw new Rejected("control rejects everything"); }
        switch (task)
        {
            case BenchTask.TypedLoad or BenchTask.GenericLoad:
                if (task == BenchTask.TypedLoad && w.TypedModel == null) throw new Unsupported("no typed model");
                return new FnRunner<int, J>(() => 0, _ => { Fail(); return Value(); }, v => new Product.Value(v));
            case BenchTask.Dump:
                return new FnRunner<int, byte[]>(() => 0, _ => { Fail(); return Encoding.UTF8.GetBytes(Value().ToJson()); },
                    b => new Product.Bytes(b));
            case BenchTask.DocumentParse:
                return new FnRunner<int, (J, byte[])>(() => 0, _ => { Fail(); return (Value(), Tweak(w.Input)); },
                    x => new Product.Document(x.Item1, x.Item2));
            case BenchTask.UnchangedEmit:
                return new FnRunner<int, byte[]>(() => 0, _ => { Fail(); return Tweak(w.Input); }, b => new Product.Bytes(b));
            case BenchTask.EditScript:
                {
                    var want = w.EditExpected ?? throw new Unsupported("no edit script");
                    return new FnRunner<int, byte[]>(() => 0, _ => { Fail(); return Tweak(want); }, b => new Product.Bytes(b));
                }
            default:
                {
                    var want = w.PatchExpected ?? throw new Unsupported("no patch");
                    return new FnRunner<int, byte[]>(() => 0, _ => { Fail(); return Tweak(want); }, b => new Product.Bytes(b),
                        () => new FailedPatch(kind != "wrong", w.Input.ToArray()));
                }
        }
    }
}
#endif
