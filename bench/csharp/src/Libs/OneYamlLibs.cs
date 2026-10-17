#if ONEYAML_LINKED
using System.Numerics;
using System.Reflection;
using OneYaml;
using OneYamlKit.Bench.Models;

namespace OneYamlKit.Bench.Libs;

/// <summary>oneyaml-csharp, built from the checkout named by KIT_ONEYAML_CSHARP_SRC.
///
/// The library has two ways to bind typed data, and they are separate rows so each is compared with the
/// strongest competitor on its own footing: <c>oneyaml-csharp</c> is the reflection binder (what a user gets
/// with no setup), <c>oneyaml-csharp-gen</c> the source-generated one (a <see cref="YamlSerializerContext"/>
/// built by the library's generator from the kit's models). The document tasks involve no binder, so both rows
/// run the same code on them.
///
/// Every library gets UTF-8 bytes and returns UTF-8 bytes. This one reads bytes directly, so nothing is decoded
/// or encoded inside the clock.</summary>
public abstract class OneYamlLibBase : ILibrary
{
    public abstract string Id { get; }
    public string Package => "OneYaml";

    // The checkout's commit, which the build stamps into the harness.
    public string Version { get; } = typeof(OneYamlLibBase).Assembly
        .GetCustomAttributes<AssemblyMetadataAttribute>()
        .FirstOrDefault(a => a.Key == "OneYamlRevision")?.Value ?? "devel";

    public Role Role => Role.Own;
    public abstract IReadOnlyDictionary<string, string> Settings { get; }

    /// <summary>The metadata for a type from the generated context, or null on the reflection row.</summary>
    protected abstract bool Generated { get; }

    public Runner Runner(BenchTask task, Workload w)
    {
        switch (task)
        {
            case BenchTask.TypedLoad:
                return w.TypedModel switch
                {
                    "gha" => Generated
                        ? new FnRunner<byte[], Workflow?>(w.OwnBytes, input => YamlSerializer.Deserialize(input, OneYamlContext.Default.Workflow),
                            t => new Product.Value(Conv.WorkflowJ(t!)))
                        : new FnRunner<byte[], Workflow?>(w.OwnBytes, input => YamlSerializer.Deserialize<Workflow>(input),
                            t => new Product.Value(Conv.WorkflowJ(t!))),
                    "records" => Generated
                        ? new FnRunner<byte[], List<Record>?>(w.OwnBytes, input => YamlSerializer.Deserialize(input, OneYamlContext.Default.ListRecord),
                            t => new Product.Value(Conv.Records(t!)))
                        : new FnRunner<byte[], List<Record>?>(w.OwnBytes, input => YamlSerializer.Deserialize<List<Record>>(input),
                            t => new Product.Value(Conv.Records(t!))),
                    _ => throw Support.No("no typed model for this workload"),
                };
            case BenchTask.GenericLoad:
                if (w.Documents != 1)
                {
                    return Generated
                        ? new FnRunner<byte[], IReadOnlyList<object?>>(w.OwnBytes, input => YamlSerializer.DeserializeAll(input, OneYamlContext.Default.Object)!,
                            l => new Product.Value(Conv.Docs(l.Select(Conv.FromObject).ToList())))
                        : new FnRunner<byte[], IReadOnlyList<object?>>(w.OwnBytes, input => YamlSerializer.DeserializeAll<object>(input)!,
                            l => new Product.Value(Conv.Docs(l.Select(Conv.FromObject).ToList())));
                }
                return Generated
                    ? new FnRunner<byte[], object?>(w.OwnBytes, input => YamlSerializer.Deserialize(input, OneYamlContext.Default.Object),
                        t => new Product.Value(Conv.FromObject(t)))
                    : new FnRunner<byte[], object?>(w.OwnBytes, input => YamlSerializer.Deserialize<object>(input),
                        t => new Product.Value(Conv.FromObject(t)));
            case BenchTask.Dump:
                // The starting state is the library's own generic tree, read from the input.
                if (w.Documents != 1) throw Support.No("dump is defined for one document");
                return Generated
                    ? new FnRunner<object?, byte[]>(() => YamlSerializer.Deserialize(w.OwnBytes(), OneYamlContext.Default.Object),
                        tree => YamlSerializer.SerializeToUtf8Bytes(tree, OneYamlContext.Default.Object), b => new Product.Bytes(b))
                    : new FnRunner<object?, byte[]>(() => YamlSerializer.Deserialize<object>(w.OwnBytes()),
                        tree => YamlSerializer.SerializeToUtf8Bytes(tree), b => new Product.Bytes(b));
            case BenchTask.DocumentParse:
                return new ParseRunner(w);
            case BenchTask.UnchangedEmit:
                return new FnRunner<YamlDocument, byte[]>(() => YamlDocument.Parse(w.OwnBytes()), d => d.ToUtf8Bytes(),
                    b => new Product.Bytes(b));
            case BenchTask.EditScript:
                {
                    // The pointers go to the library as they are: its pointer methods take RFC 6901. Values are
                    // converted before the clock.
                    var edits = w.EditOps.Select(o => (o.Name, o.Path, Value: o.Value == null ? null : ToObject(o.Value))).ToList();
                    if (edits.Any(e => e.Name is not ("replace" or "remove"))) throw Support.No("edit scripts hold replace and remove only");
                    return new FnRunner<YamlDocument, byte[]>(() => YamlDocument.Parse(w.OwnBytes()), d => Edit(d, edits),
                        b => new Product.Bytes(b));
                }
            default:
                {
                    if (w.Documents != 1) throw Support.No("a patch addresses one document");
                    // The patch is built once, outside the clock, as the library's own patch object.
                    var patch = YamlPatch.Parse(PatchJson(w.PatchOps));
                    var failing = YamlPatch.Parse(PatchJson(w.PatchFailingOps));
                    return new FnRunner<YamlDocument, byte[]>(
                        () => YamlDocument.Parse(w.OwnBytes()),
                        d =>
                        {
                            using (d)
                            {
                                d.ApplyPatch(patch);
                                return d.ToUtf8Bytes();
                            }
                        },
                        b => new Product.Bytes(b),
                        () =>
                        {
                            // The library refuses a patch with a failing step and leaves the document as it was.
                            using var d = YamlDocument.Parse(w.Input);
                            bool errored = false;
                            try { d.ApplyPatch(failing); }
                            catch (YamlException) { errored = true; }
                            return new FailedPatch(errored, d.ToUtf8Bytes());
                        });
                }
        }
    }

    static string PatchJson(List<Op> ops) =>
        "[" + string.Join(",", ops.Select(o =>
            $"{{\"op\":\"{o.Name}\",\"path\":{JsonStr(o.Path)}" + (o.Value == null ? "" : ",\"value\":" + o.Value.ToJson()) + "}")) + "]";

    static string JsonStr(string s) => System.Text.Json.JsonSerializer.Serialize(s);

    // The document is consumed: it is edited, written and released, as a caller does with a using block.
    static byte[] Edit(YamlDocument doc, List<(string Name, string Path, object? Value)> edits)
    {
        using (doc)
        {
            foreach (var (name, path, value) in edits)
            {
                if (name == "remove") doc.Remove(path);
                else doc.Set(path, value);
            }
            return doc.ToUtf8Bytes();
        }
    }

    /// <summary>A common value as the plain objects the document's setters take.</summary>
    static object? ToObject(J v) => v switch
    {
        J.Null => null,
        J.Bool b => b.V,
        J.Str s => s.V,
        J.Int i => i.V >= long.MinValue && i.V <= long.MaxValue ? (object)(long)i.V : i.V,
        J.Float f => f.V,
        J.Arr a => a.V.Select(ToObject).ToList(),
        J.Obj o => o.V.ToDictionary(kv => kv.Key, kv => ToObject(kv.Value)),
        _ => throw new InvalidOperationException("not a value"),
    };

    // ---- the document tree as a common value, for the guard ---------------------------------------

    internal static J Tree(YamlDocument doc)
    {
        var docs = new List<J>();
        for (int i = 0; i < doc.Documents.Count; i++) docs.Add(NodeJ(doc.Documents[i]));
        return Conv.Docs(docs);
    }

    static J NodeJ(YamlNode n)
    {
        if (n.Kind == YamlNodeKind.Alias) n = n.Target;
        switch (n.Kind)
        {
            case YamlNodeKind.Mapping:
                {
                    var o = new J.Obj();
                    foreach (var e in n.Entries) o.Put(KeyText(e.Key), NodeJ(e.Value));
                    return o;
                }
            case YamlNodeKind.Sequence:
                return new J.Arr(n.Items.Select(NodeJ).ToList());
            default:
                return n.GetValue<object>() switch
                {
                    null => J.Null.I,
                    bool b => new J.Bool(b),
                    string s => new J.Str(s),
                    long l => new J.Int(l),
                    BigInteger bi => new J.Int(bi),
                    double d => new J.Float(d),
                    var other => throw new InvalidOperationException("the harness cannot read a " + other.GetType().FullName),
                };
        }
    }

    static string KeyText(YamlNode key)
    {
        if (key.Kind == YamlNodeKind.Alias) key = key.Target;
        return key.Kind == YamlNodeKind.Scalar ? key.GetValue<string>() ?? "" : throw new InvalidOperationException("a collection is a key");
    }
}

/// <summary>document-parse: parse and release, as a <c>using</c> block does. The document's buffers come from a
/// pool and go back on release, so a run that never released would measure an allocation a caller does not make.</summary>
sealed class ParseRunner(Workload w) : Runner
{
    public override Product Check()
    {
        using var doc = YamlDocument.Parse(w.Input);
        return new Product.Document(OneYamlLibBase.Tree(doc), doc.ToUtf8Bytes());
    }

    // Each iteration parses its own copy of the input, made here and not in the batch.
    public override Batch Prepare(int n)
    {
        var inputs = new byte[n][];
        for (int i = 0; i < n; i++) inputs[i] = w.OwnBytes();
        return new ParseBatch(inputs);
    }

    sealed class ParseBatch(byte[][] inputs) : Batch
    {
        public override IReadOnlyList<object?> States => inputs;

        public override void Run()
        {
            foreach (var input in inputs)
            {
                using var doc = YamlDocument.Parse(input);
            }
        }
    }
}

public sealed class OneYamlReflectionLib : OneYamlLibBase
{
    public override string Id => "oneyaml-csharp";
    protected override bool Generated => false;

    public override IReadOnlyDictionary<string, string> Settings { get; } = new Dictionary<string, string>
    {
        ["binding"] = "reflection (YamlSerializer.Deserialize<T> with default options)",
        ["input and output"] = "UTF-8 bytes in and out, read and written directly",
        ["limits"] = "defaults, not raised",
        ["documents"] = "YamlDocument.Parse; edits through Set and Remove on RFC 6901 pointers; the document is released after each run",
    };
}

public sealed class OneYamlGeneratedLib : OneYamlLibBase
{
    public override string Id => "oneyaml-csharp-gen";
    protected override bool Generated => true;

    public override IReadOnlyDictionary<string, string> Settings { get; } = new Dictionary<string, string>
    {
        ["binding"] = "source-generated (a YamlSerializerContext built by the library's generator from the kit's models)",
        ["input and output"] = "UTF-8 bytes in and out, read and written directly",
        ["limits"] = "defaults, not raised",
        ["documents"] = "same code as the reflection row: no binder is involved",
    };
}
#endif
