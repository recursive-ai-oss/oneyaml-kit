using System.Text.Json;
using Json.Patch;
using Yaml2JsonNode;
using OneYamlKit.Bench.Models;

namespace OneYamlKit.Bench.Libs;

/// <summary>VYaml, driven through the API its documentation shows: <c>YamlSerializer</c> over UTF-8 bytes,
/// typed models marked <c>[YamlObject]</c> so its source generator writes their formatters (the way its
/// users take its speed), and <c>object</c> for untyped data. It has no lossless document and no patch engine.</summary>
public sealed class VYamlLib : ILibrary
{
    public string Id => "vyaml";
    public string Package => "VYaml";
    public string Version => Support.Version("VYaml");
    public Role Role => Role.Competitor;

    public IReadOnlyDictionary<string, string> Settings { get; } = new Dictionary<string, string>
    {
        ["api"] = "VYaml.Serialization.YamlSerializer over UTF-8 bytes, source-generated formatters for the typed models",
        ["input"] = "the input bytes as they are (the library reads UTF-8); output bytes are what the library writes",
        ["limits"] = "defaults",
    };

    public Runner Runner(BenchTask task, Workload w)
    {
        var bytes = new ReadOnlyMemory<byte>(w.Input);
        bool single = w.Documents == 1;
        switch (task)
        {
            case BenchTask.TypedLoad:
                return w.TypedModel switch
                {
                    "gha" => new FnRunner<int, Workflow>(() => 0, _ => VYaml.Serialization.YamlSerializer.Deserialize<Workflow>(bytes),
                        t => new Product.Value(Conv.WorkflowJ(t))),
                    "records" => new FnRunner<int, List<Record>>(() => 0, _ => VYaml.Serialization.YamlSerializer.Deserialize<List<Record>>(bytes),
                        t => new Product.Value(Conv.Records(t))),
                    _ => throw Support.No("no typed model for this workload"),
                };
            case BenchTask.GenericLoad:
                if (single)
                    return new FnRunner<int, object?>(() => 0, _ => VYaml.Serialization.YamlSerializer.Deserialize<object>(bytes),
                        t => new Product.Value(Conv.FromObject(t)));
                return new FnRunner<int, List<object>>(() => 0,
                    _ => VYaml.Serialization.YamlSerializer.DeserializeMultipleDocuments<object>(bytes).ToList(),
                    docs => new Product.Value(Conv.Docs(docs.Select(Conv.FromObject).ToList())));
            case BenchTask.Dump:
                if (!single) throw Support.No("dump is defined for one document");
                return new FnRunner<object?, ReadOnlyMemory<byte>>(() => VYaml.Serialization.YamlSerializer.Deserialize<object>(bytes),
                    tree => VYaml.Serialization.YamlSerializer.Serialize<object?>(tree),
                    m => new Product.Bytes(m.ToArray()));
            case BenchTask.DocumentParse or BenchTask.UnchangedEmit or BenchTask.EditScript:
                throw Support.No("no lossless document: comments and layout are discarded");
            default:
                throw Support.No("no patch engine");
        }
    }
}

/// <summary>Yaml2JsonNode (YAML to System.Text.Json nodes and back, on YamlDotNet) with JsonPatch.Net:
/// the only RFC 6902 route in the .NET libraries. It works on data, not on text, so what it writes after a
/// patch is a fresh serialisation of the tree and not the edited source; it is a baseline for what the
/// patch task costs when the document is not kept, not a lossless editor.</summary>
public sealed class Yaml2JsonNodeLib : ILibrary
{
    public string Id => "yaml2jsonnode";
    public string Package => "Yaml2JsonNode";
    public string Version => Support.Version("Yaml2JsonNode");
    public Role Role => Role.Competitor;

    public IReadOnlyDictionary<string, string> Settings { get; } = new Dictionary<string, string>
    {
        ["api"] = "Yaml2JsonNode.YamlSerializer.Parse / ToJsonNode / ToYamlNode / Serialize, System.Text.Json for typed models",
        ["patch"] = "JsonPatch.Net " + Support.Version("JsonPatch.Net") + " on the JsonNode tree, then written back as YAML; not lossless",
        ["yaml parser"] = "YamlDotNet " + Support.Version("YamlDotNet"),
    };

    static readonly JsonSerializerOptions Typed = new() { PropertyNamingPolicy = JsonNamingPolicy.CamelCase };

    static string Write(System.Text.Json.Nodes.JsonNode? node) =>
        Yaml2JsonNode.YamlSerializer.Serialize(node!.ToYamlNode(), null);

    public Runner Runner(BenchTask task, Workload w)
    {
        var text = w.InputText;
        bool single = w.Documents == 1;
        switch (task)
        {
            case BenchTask.TypedLoad:
                return w.TypedModel switch
                {
                    "gha" => new FnRunner<int, Workflow?>(() => 0, _ => Yaml2JsonNode.YamlSerializer.Deserialize<Workflow>(text, Typed),
                        t => new Product.Value(Conv.WorkflowJ(t!))),
                    "records" => new FnRunner<int, List<Record>?>(() => 0, _ => Yaml2JsonNode.YamlSerializer.Deserialize<List<Record>>(text, Typed),
                        t => new Product.Value(Conv.Records(t!))),
                    _ => throw Support.No("no typed model for this workload"),
                };
            case BenchTask.GenericLoad:
                return new FnRunner<int, List<System.Text.Json.Nodes.JsonNode?>>(() => 0,
                    _ => Yaml2JsonNode.YamlSerializer.Parse(text).ToJsonNode().ToList(),
                    docs => new Product.Value(Conv.Docs(docs.Select(Support.FromJsonNode).ToList())));
            case BenchTask.Dump:
                if (!single) throw Support.No("dump is defined for one document");
                return new FnRunner<System.Text.Json.Nodes.JsonNode?, string>(
                    () => Yaml2JsonNode.YamlSerializer.Parse(text).ToJsonNode().First(), Write,
                    s => new Product.Bytes(Support.Utf8.GetBytes(s)));
            case BenchTask.DocumentParse or BenchTask.UnchangedEmit or BenchTask.EditScript:
                throw Support.No("no lossless document: the tree is data, and comments and layout are discarded");
            default:
                {
                    if (!single) throw Support.No("a patch addresses one document");
                    var patch = BuildPatch(w.PatchOps);
                    var failing = BuildPatch(w.PatchFailingOps);
                    System.Text.Json.Nodes.JsonNode? Load() => Yaml2JsonNode.YamlSerializer.Parse(text).ToJsonNode().First();
                    return new FnRunner<System.Text.Json.Nodes.JsonNode?, string>(
                        Load,
                        doc =>
                        {
                            // JsonPatch.Net applies the operations to a copy and reports failure in its result.
                            var r = patch.Apply(doc);
                            if (!r.IsSuccess) throw new Rejected(r.Error ?? "the patch failed");
                            return Write(r.Result);
                        },
                        s => new Product.Bytes(Support.Utf8.GetBytes(s)),
                        () =>
                        {
                            var doc = Load();
                            var r = failing.Apply(doc);
                            return new FailedPatch(!r.IsSuccess, Support.Utf8.GetBytes(Write(doc)));
                        });
                }
        }
    }

    /// <summary>The operations as the library's own patch object, built once, outside the clock.</summary>
    static JsonPatch BuildPatch(List<Op> ops)
    {
        var arr = new System.Text.Json.Nodes.JsonArray();
        foreach (var op in ops)
        {
            var o = new System.Text.Json.Nodes.JsonObject { ["op"] = op.Name, ["path"] = op.Path };
            if (op.Value != null) o["value"] = Support.ToJsonNode(op.Value);
            arr.Add(o);
        }
        return JsonSerializer.Deserialize<JsonPatch>(arr.ToJsonString())!;
    }
}
