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
        ["serializer"] = "WithQuotingNecessaryStrings (the library's own option for writing a string that reads as a number or a boolean in quotes, so the text reads back as the same value)",
        ["input and output"] = "UTF-8 bytes in and out: the text is decoded and encoded inside the timed work, as it is for every library that works on strings",
        ["limits"] = "defaults",
    };

    // The builders are made once, as a program does; the first call (the guard's) pays for their set-up.
    static readonly IDeserializer D = new DeserializerBuilder()
        .WithNamingConvention(CamelCaseNamingConvention.Instance)
        .WithAttemptingUnquotedStringTypeDeserialization()
        .Build();

    static readonly ISerializer S = new SerializerBuilder().WithQuotingNecessaryStrings().Build();

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
        bool single = w.Documents == 1;
        switch (task)
        {
            case BenchTask.TypedLoad:
                return w.TypedModel switch
                {
                    "gha" => new FnRunner<byte[], Workflow>(w.OwnBytes, input => D.Deserialize<Workflow>(Support.Utf8.GetString(input)), t => new Product.Value(Conv.WorkflowJ(t))),
                    "records" => new FnRunner<byte[], List<Record>>(w.OwnBytes, input => D.Deserialize<List<Record>>(Support.Utf8.GetString(input)), t => new Product.Value(Conv.Records(t))),
                    _ => throw Support.No("no typed model for this workload"),
                };
            case BenchTask.GenericLoad:
                return new FnRunner<byte[], List<object?>>(w.OwnBytes, input => LoadAll(Support.Utf8.GetString(input), single),
                    docs => new Product.Value(Conv.Docs(docs.Select(Conv.FromObject).ToList())));
            case BenchTask.Dump:
                if (!single) throw Support.No("dump is defined for one document");
                return new FnRunner<object?, byte[]>(() => D.Deserialize<object>(w.OwnText()), tree => Support.Utf8.GetBytes(S.Serialize(tree!)),
                    b => new Product.Bytes(b));
            case BenchTask.DocumentParse or BenchTask.UnchangedEmit or BenchTask.EditScript:
                throw Support.No("no lossless document: comments and layout are discarded");
            default:
                throw Support.No("no patch engine");
        }
    }
}
