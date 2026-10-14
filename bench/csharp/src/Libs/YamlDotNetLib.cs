using YamlDotNet.Core;
using YamlDotNet.Core.Events;
using YamlDotNet.Serialization;
using YamlDotNet.Serialization.NamingConventions;
using OneYamlKit.Bench.Models;

namespace OneYamlKit.Bench.Libs;

/// <summary>YamlDotNet, driven through the API its documentation shows: <c>DeserializerBuilder</c> and
/// <c>SerializerBuilder</c>, with reflection (the normal path; its static generator is a separate package
/// that few users add). It has no lossless document and no patch engine.</summary>
public sealed class YamlDotNetLib : ILibrary
{
    public string Id => "yamldotnet";
    public string Package => "YamlDotNet";
    public string Version => Support.Version("YamlDotNet");
    public Role Role => Role.Competitor;

    public IReadOnlyDictionary<string, string> Settings { get; } = new Dictionary<string, string>
    {
        ["api"] = "DeserializerBuilder / SerializerBuilder, reflection",
        ["deserializer"] = "camel-case naming, WithAttemptingUnquotedStringTypeDeserialization (so untyped scalars are numbers and booleans, not strings)",
        ["limits"] = "defaults",
    };

    // The builders are made once, as a program does; the first call (the guard's) pays for their set-up.
    static readonly IDeserializer D = new DeserializerBuilder()
        .WithNamingConvention(CamelCaseNamingConvention.Instance)
        .WithAttemptingUnquotedStringTypeDeserialization()
        .Build();

    static readonly ISerializer S = new SerializerBuilder().Build();

    /// <summary>Every document of a stream as a generic tree: one document through the string API, several
    /// through the parser.</summary>
    static List<object?> LoadAll(string text, bool single)
    {
        if (single) return [D.Deserialize<object>(text)];
        var docs = new List<object?>();
        var parser = new Parser(new StringReader(text));
        parser.Consume<StreamStart>();
        while (parser.Accept<DocumentStart>(out _)) docs.Add(D.Deserialize<object>(parser));
        return docs;
    }

    public Runner Runner(BenchTask task, Workload w)
    {
        var text = w.InputText;
        bool single = w.Documents == 1;
        switch (task)
        {
            case BenchTask.TypedLoad:
                return w.TypedModel switch
                {
                    "gha" => new FnRunner<int, Workflow>(() => 0, _ => D.Deserialize<Workflow>(text), t => new Product.Value(Conv.WorkflowJ(t))),
                    "records" => new FnRunner<int, List<Record>>(() => 0, _ => D.Deserialize<List<Record>>(text), t => new Product.Value(Conv.Records(t))),
                    _ => throw Support.No("no typed model for this workload"),
                };
            case BenchTask.GenericLoad:
                return new FnRunner<int, List<object?>>(() => 0, _ => LoadAll(text, single),
                    docs => new Product.Value(Conv.Docs(docs.Select(Conv.FromObject).ToList())));
            case BenchTask.Dump:
                if (!single) throw Support.No("dump is defined for one document");
                return new FnRunner<object?, string>(() => D.Deserialize<object>(text), tree => S.Serialize(tree!),
                    s => new Product.Bytes(Support.Utf8.GetBytes(s)));
            case BenchTask.DocumentParse or BenchTask.UnchangedEmit or BenchTask.EditScript:
                throw Support.No("no lossless document: comments and layout are discarded");
            default:
                throw Support.No("no patch engine");
        }
    }
}
