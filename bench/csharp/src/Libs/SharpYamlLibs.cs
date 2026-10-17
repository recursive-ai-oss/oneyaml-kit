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
        ["limits"] = "defaults (MaxDepth 64), not raised",
        ["input and output"] = "UTF-8 bytes in and out: the text is decoded and encoded inside the timed work, as it is for every library that works on strings",
    };

    // Aliases are refused when reading into `object` unless references are preserved, and the workloads
    // include anchors, so reading untyped data needs the option; writing a tree does not.
    static readonly YamlSerializerOptions LoadOptions = new() { ReferenceHandling = YamlReferenceHandling.Preserve };
    static readonly YamlSerializerOptions DumpOptions = new();

    public Runner Runner(BenchTask task, Workload w)
    {
        switch (task)
        {
            case BenchTask.TypedLoad:
                return w.TypedModel switch
                {
                    "gha" => new FnRunner<byte[], Workflow?>(w.OwnBytes, input => YamlSerializer.Deserialize(Support.Utf8.GetString(input), SharpYamlContext.Default.Workflow),
                        t => new Product.Value(Conv.WorkflowJ(t!))),
                    "records" => new FnRunner<byte[], List<Record>?>(w.OwnBytes, input => YamlSerializer.Deserialize(Support.Utf8.GetString(input), SharpYamlContext.Default.ListRecord),
                        t => new Product.Value(Conv.Records(t!))),
                    _ => throw Support.No("no typed model for this workload"),
                };
            case BenchTask.GenericLoad:
                if (w.Documents != 1) throw Support.No("the serializer reads one document per call; the model API reads a stream");
                return new FnRunner<byte[], object?>(w.OwnBytes, input => YamlSerializer.Deserialize<object>(Support.Utf8.GetString(input), LoadOptions),
                    t => new Product.Value(Conv.FromObject(t)));
            case BenchTask.Dump:
                if (w.Documents != 1) throw Support.No("dump is defined for one document");
                return new FnRunner<object?, byte[]>(() => YamlSerializer.Deserialize<object>(w.OwnText(), LoadOptions),
                    tree => Support.Utf8.GetBytes(YamlSerializer.Serialize<object?>(tree, DumpOptions)),
                    b => new Product.Bytes(b));
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
        ["limits"] = "defaults (MaxDepth 64), not raised",
        ["input and output"] = "UTF-8 bytes in and out: the text is decoded and encoded inside the timed work, as it is for every library that works on strings",
    };

    public Runner Runner(BenchTask task, Workload w)
    {
        switch (task)
        {
            case BenchTask.GenericLoad:
                return new FnRunner<byte[], YamlStream>(w.OwnBytes, input => YamlStream.Load(new StringReader(Support.Utf8.GetString(input))),
                    s => new Product.Value(Conv.Docs(s.Select(d => ModelJ(d.Contents)).ToList())));
            case BenchTask.Dump:
                if (w.Documents != 1) throw Support.No("dump is defined for one document");
                return new FnRunner<YamlStream, byte[]>(() => YamlStream.Load(new StringReader(w.OwnText())),
                    s => Support.Utf8.GetBytes(s[0].Contents!.ToString()),
                    b => new Product.Bytes(b));
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
        ["edits"] = "the timed work finds each edit's node in the parsed tree's tokens, writes the new value's text (style, quoting, block header, chomping) and applies one WithTextChange per edit, last edit first; the library has no path or value API, so that lookup and writing are the harness's own code and the cell is reported as variant harness-rendered",
        ["limits"] = "defaults (MaxDepth 64), not raised",
        ["input and output"] = "UTF-8 bytes in and out: the text is decoded and encoded inside the timed work, as it is for every library that works on strings",
    };

    static readonly YamlSyntaxOptions Options = new() { IncludeTrivia = true };

    /// <summary>The variant of an edit whose lookup and new text are the harness's code, not the library's: the
    /// library only replaces a range of characters. Such a cell is shown marked and is never called the best of
    /// a row; a row is still compared with it when it is ahead.</summary>
    public const string HarnessRendered = "harness-rendered";

    public Runner Runner(BenchTask task, Workload w)
    {
        switch (task)
        {
            case BenchTask.DocumentParse:
                return new FnRunner<byte[], YamlSyntaxTree>(w.OwnBytes, input => YamlSyntaxTree.Parse(Support.Utf8.GetString(input), Options),
                    t => new Product.Document(null, Support.Utf8.GetBytes(t.ToFullString())));
            case BenchTask.UnchangedEmit:
                return new FnRunner<YamlSyntaxTree, byte[]>(() => YamlSyntaxTree.Parse(w.OwnText(), Options),
                    t => Support.Utf8.GetBytes(t.ToFullString()),
                    b => new Product.Bytes(b));
            case BenchTask.EditScript:
                {
                    var ops = w.EditOps;
                    // A trial on a throw-away tree finds out, before anything is timed, whether the harness can
                    // express these edits at all.
                    SharpYamlEdits.Apply(YamlSyntaxTree.Parse(w.InputText, Options), ops);
                    return new FnRunner<YamlSyntaxTree, byte[]>(() => YamlSyntaxTree.Parse(w.OwnText(), Options),
                        t => Support.Utf8.GetBytes(SharpYamlEdits.Apply(t, ops).ToFullString()),
                        b => new Product.Bytes(b), variant: HarnessRendered);
                }
            case BenchTask.TypedLoad or BenchTask.GenericLoad or BenchTask.Dump:
                throw Support.No("the syntax tree gives no values; see sharpyaml");
            default:
                throw Support.No("no patch engine");
        }
    }
}
