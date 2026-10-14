using SharpYaml;
using SharpYaml.Model;
using SharpYaml.Syntax;
using OneYamlKit.Bench.Models;

namespace OneYamlKit.Bench.Libs;

/// <summary>SharpYaml's serializer, the System.Text.Json-style surface most of its users take: typed models
/// through a source-generated context (what its documentation recommends, and what makes it fast), and
/// untyped values through <c>Deserialize&lt;object&gt;</c>. It reads one document per call.</summary>
public sealed class SharpYamlLib : ILibrary
{
    public string Id => "sharpyaml";
    public string Package => "SharpYaml";
    public string Version => Support.Version("SharpYaml");
    public Role Role => Role.Competitor;

    public IReadOnlyDictionary<string, string> Settings { get; } = new Dictionary<string, string>
    {
        ["api"] = "YamlSerializer, typed models through a source-generated YamlSerializerContext, untyped as object",
        ["untyped load"] = "ReferenceHandling = Preserve (the documented requirement for any input with aliases), schema Core",
        ["limits"] = "defaults (MaxDepth 64)",
    };

    // Aliases are refused when reading into `object` unless references are preserved, and the workloads
    // include anchors, so reading untyped data needs the option; writing a tree does not.
    static readonly YamlSerializerOptions LoadOptions = new() { ReferenceHandling = YamlReferenceHandling.Preserve };
    static readonly YamlSerializerOptions DumpOptions = new();

    public Runner Runner(BenchTask task, Workload w)
    {
        var text = w.InputText;
        switch (task)
        {
            case BenchTask.TypedLoad:
                return w.TypedModel switch
                {
                    "gha" => new FnRunner<int, Workflow?>(() => 0, _ => YamlSerializer.Deserialize(text, SharpYamlContext.Default.Workflow),
                        t => new Product.Value(Conv.WorkflowJ(t!))),
                    "records" => new FnRunner<int, List<Record>?>(() => 0, _ => YamlSerializer.Deserialize(text, SharpYamlContext.Default.ListRecord),
                        t => new Product.Value(Conv.Records(t!))),
                    _ => throw Support.No("no typed model for this workload"),
                };
            case BenchTask.GenericLoad:
                if (w.Documents != 1) throw Support.No("the serializer reads one document per call; the model API reads a stream");
                return new FnRunner<int, object?>(() => 0, _ => YamlSerializer.Deserialize<object>(text, LoadOptions),
                    t => new Product.Value(Conv.FromObject(t)));
            case BenchTask.Dump:
                if (w.Documents != 1) throw Support.No("dump is defined for one document");
                return new FnRunner<object?, string>(() => YamlSerializer.Deserialize<object>(text, LoadOptions),
                    tree => YamlSerializer.Serialize<object?>(tree, DumpOptions),
                    s => new Product.Bytes(Support.Utf8.GetBytes(s)));
            case BenchTask.DocumentParse or BenchTask.UnchangedEmit or BenchTask.EditScript:
                throw Support.No("the serializer keeps no comments or layout; see sharpyaml-syntax");
            default:
                throw Support.No("no patch engine");
        }
    }
}

/// <summary>SharpYaml's node model (<c>YamlStream</c>): a document tree that keeps scalar text, style and
/// source marks but not comments or blank lines. Every document of a stream.</summary>
public sealed class SharpYamlModelLib : ILibrary
{
    public string Id => "sharpyaml-model";
    public string Package => "SharpYaml";
    public string Version => Support.Version("SharpYaml");
    public Role Role => Role.Competitor;

    public IReadOnlyDictionary<string, string> Settings { get; } = new Dictionary<string, string>
    {
        ["api"] = "SharpYaml.Model.YamlStream.Load; nodes written back with YamlNode.ToString",
        ["values"] = "the tree holds scalar text; the harness resolves plain scalars by the core schema when it reads the tree",
    };

    public Runner Runner(BenchTask task, Workload w)
    {
        var text = w.InputText;
        switch (task)
        {
            case BenchTask.GenericLoad:
                return new FnRunner<int, YamlStream>(() => 0, _ => YamlStream.Load(new StringReader(text)),
                    s => new Product.Value(Conv.Docs(s.Select(d => ModelJ(d.Contents)).ToList())));
            case BenchTask.Dump:
                if (w.Documents != 1) throw Support.No("dump is defined for one document");
                return new FnRunner<YamlStream, string>(() => YamlStream.Load(new StringReader(text)),
                    s => s[0].Contents!.ToString(),
                    s => new Product.Bytes(Support.Utf8.GetBytes(s)));
            case BenchTask.TypedLoad:
                throw Support.No("the node model is untyped; see sharpyaml");
            case BenchTask.DocumentParse or BenchTask.UnchangedEmit or BenchTask.EditScript:
                throw Support.No("the node model discards comments and blank lines; see sharpyaml-syntax");
            default:
                throw Support.No("no patch engine");
        }
    }

    static J ModelJ(YamlElement? e)
    {
        switch (e)
        {
            case null: return J.Null.I;
            case YamlValue v:
                if (v.Tag == "tag:yaml.org,2002:str" || v.Style != ScalarStyle.Plain) return new J.Str(v.Value ?? "");
                return Referee.Resolve(v.Value ?? "");
            case YamlSequence q: return new J.Arr(q.Select(ModelJ).ToList());
            case YamlMapping m:
                {
                    var o = new J.Obj();
                    foreach (var kv in m) o.Put(J.KeyText(ModelJ(kv.Key)), ModelJ(kv.Value));
                    return o;
                }
            default: throw new InvalidOperationException("unknown node " + e.GetType().Name);
        }
    }
}

/// <summary>SharpYaml's syntax tree: the lossless layer. It keeps every character of the source, finds
/// scalars by position, and applies an edit as a raw text change that is re-parsed and validated. It has no
/// notion of a path or a value, so the harness works out each edit's span and replacement text before the
/// clock (see <see cref="SharpYamlEdits"/>); the clock sees only the text changes.</summary>
public sealed class SharpYamlSyntaxLib : ILibrary
{
    public string Id => "sharpyaml-syntax";
    public string Package => "SharpYaml";
    public string Version => Support.Version("SharpYaml");
    public Role Role => Role.Competitor;

    public IReadOnlyDictionary<string, string> Settings { get; } = new Dictionary<string, string>
    {
        ["api"] = "SharpYaml.Syntax.YamlSyntaxTree.Parse (IncludeTrivia), ToFullString, WithTextChange",
        ["document value"] = "the tree gives no value, so document-parse is judged on the bytes it writes back",
        ["edits"] = "span and replacement text are worked out from the workload's pointers before the clock; the timed work is one WithTextChange per edit, last edit first",
    };

    static readonly YamlSyntaxOptions Options = new() { IncludeTrivia = true };

    public Runner Runner(BenchTask task, Workload w)
    {
        var text = w.InputText;
        switch (task)
        {
            case BenchTask.DocumentParse:
                return new FnRunner<int, YamlSyntaxTree>(() => 0, _ => YamlSyntaxTree.Parse(text, Options),
                    t => new Product.Document(null, Support.Utf8.GetBytes(t.ToFullString())));
            case BenchTask.UnchangedEmit:
                return new FnRunner<YamlSyntaxTree, string>(() => YamlSyntaxTree.Parse(text, Options), t => t.ToFullString(),
                    s => new Product.Bytes(Support.Utf8.GetBytes(s)));
            case BenchTask.EditScript:
                {
                    var edits = SharpYamlEdits.Plan(w);
                    return new FnRunner<YamlSyntaxTree, string>(() => YamlSyntaxTree.Parse(text, Options),
                        t =>
                        {
                            foreach (var (start, length, raw) in edits) t = t.WithTextChange(start, length, raw);
                            return t.ToFullString();
                        },
                        s => new Product.Bytes(Support.Utf8.GetBytes(s)));
                }
            case BenchTask.TypedLoad or BenchTask.GenericLoad or BenchTask.Dump:
                throw Support.No("the syntax tree gives no values; see sharpyaml");
            default:
                throw Support.No("no patch engine");
        }
    }
}
