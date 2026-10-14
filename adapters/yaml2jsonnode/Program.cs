using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Json.Patch;
using Json.Pointer;
using OneYamlKit.Adapter;
using Yaml2JsonNode;
using YamlDotNet.Core;
using YamlDotNet.RepresentationModel;

return Host.Serve(new Yaml2JsonNodeKitAdapter());

/// <summary>Yaml2JsonNode (YAML to System.Text.Json nodes and back) with JsonPatch.Net applying RFC 6902 to the
/// JSON tree. This is the only RFC 6902 route in the .NET ecosystem, and it works on data, not on text, so
/// the document it writes back is a fresh serialisation. The library has no event API.</summary>
sealed class Yaml2JsonNodeKitAdapter : IAdapter
{
    public string Name => "yaml2jsonnode";
    public string Version { get; } = PackageVersions.Of("Yaml2JsonNode");

    public string[] Capabilities =>
    [
        "value", "patch:add", "patch:remove", "patch:replace", "patch:move", "patch:copy", "patch:test",
        "error-category",
    ];

    // decode: Yaml2JsonNode reads a string, so byte order marks, UTF-16/32 and malformed UTF-8 are the adapter's.
    // classification: JsonPatch.Net reports a failed operation as a message and an operation number; the
    //   adapter calls a failed `test` patch-test and any other failed operation patch-path.
    // error: the adapter itself reports a document index that does not exist.
    public string[] Emulates => ["decode", "classification", "error"];

    public IReadOnlyDictionary<string, string> Info { get; } = new Dictionary<string, string>
    {
        ["value"] = "YamlSerializer.Parse(text).ToJsonNode() for every document",
        ["patch"] = "JsonSerializer.Deserialize<JsonPatch>, JsonPatch.Apply on the JsonNode, then YamlSerializer.Serialize(ToYamlNode()); every document of the stream is written again",
        ["json patch"] = "JsonPatch.Net " + PackageVersions.Of("JsonPatch.Net") + ", JsonPointer.Net " + PackageVersions.Of("JsonPointer.Net"),
        ["yaml parser"] = "YamlDotNet " + PackageVersions.Of("YamlDotNet") + " (pinned in this adapter; Yaml2JsonNode only requires 16.3.0 or newer)",
        ["output encoding"] = "UTF-8 without a byte order mark",
        ["library stack"] = $"{Host.LibraryStackBytes / (1024 * 1024)} MiB thread",
        ["runtime"] = System.Runtime.InteropServices.RuntimeInformation.FrameworkDescription,
    };

    public Outcome Run(Request r)
    {
        if (r.Profile != "core") return Outcome.Unsupported("profile " + r.Profile);
        if (r.Op != "value" && r.Op != "patch") return Outcome.Unsupported("operation " + r.Op);
        if (r.Op == "patch" && r.Args.TryGetProperty("format", out var f) && f.GetString() != "json-patch")
            return Outcome.Unsupported("merge patch");

        var d = InputDecoder.Decode(r.Input);
        if (d.Failure != null)
            return (r.Op == "patch" ? Outcome.Error(d.Failure, output: r.Input) : Outcome.Error(d.Failure)).Emulating("decode");
        var o = r.Op == "value" ? Value(d.Text!) : Patch(d.Text!, r, d);
        return d.Transformed ? o.Emulating("decode") : o;
    }

    static Failure Describe(Exception e)
    {
        // The category comes from the exception type the library chose; its message is never read.
        var category = e switch
        {
            SyntaxErrorException or SemanticErrorException => "syntax",
            PointerParseException or JsonException => "patch-invalid",
            _ => "other",
        };
        if (e is YamlException y && y.Start.Line > 0) return new Failure(category, e.Message, (int)y.Start.Line, (int)y.Start.Column);
        return new Failure(category, e.Message);
    }

    static (List<JsonNode?> docs, Exception? failure) Load(string text) =>
        Host.Library(() =>
        {
            try { return (YamlSerializer.Parse(text).ToJsonNode().ToList<JsonNode?>(), (Exception?)null); }
            catch (Exception e) { return ([], e); }
        });

    static Outcome Value(string text)
    {
        var (docs, failure) = Load(text);
        if (failure != null) return Outcome.Error(Describe(failure));
        try
        {
            var json = Host.Convert(() =>
            {
                var budget = new Budget();
                var sb = new StringBuilder("[");
                for (int i = 0; i < docs.Count; i++)
                {
                    if (i > 0) sb.Append(',');
                    sb.Append(Convert(docs[i], budget).ToJson());
                }
                return sb.Append(']').ToString();
            });
            return Outcome.ValueOk(json);
        }
        catch (TooLargeException e) { return Outcome.Unsupported(e.Message); }
    }

    static KV Convert(JsonNode? n, Budget b)
    {
        b.Spend();
        switch (n)
        {
            case null: return KV.Null.Instance;
            case JsonObject o:
                {
                    var pairs = new List<KeyValuePair<KV, KV>>();
                    foreach (var kv in o) pairs.Add(new(new KV.Str(kv.Key), Convert(kv.Value, b)));
                    return new KV.Map(pairs);
                }
            case JsonArray a:
                {
                    var items = new List<KV>();
                    foreach (var x in a) items.Add(Convert(x, b));
                    return new KV.Seq(items);
                }
            case JsonValue v:
                switch (v.GetValue<object>())
                {
                    case string s: return new KV.Str(s);
                    case bool x: return new KV.Bool(x);
                    case decimal m:
                        // The library keeps every number as a decimal; one with a fractional scale is a float
                        // and one without is an integer, which is all the information it retains.
                        return (decimal.GetBits(m)[3] >> 16 & 0xFF) == 0
                            ? new KV.Int(m.ToString(System.Globalization.CultureInfo.InvariantCulture))
                            : new KV.Float((double)m);
                    case double d: return new KV.Float(d);
                    case long or int or short or byte or sbyte or ulong or uint or ushort:
                        return new KV.Int(System.Convert.ToString(v.GetValue<object>(), System.Globalization.CultureInfo.InvariantCulture)!);
                    case JsonElement e when e.ValueKind == JsonValueKind.Number:
                        return e.GetRawText().IndexOfAny(['.', 'e', 'E']) >= 0 ? new KV.Float(e.GetDouble()) : new KV.Int(e.GetRawText());
                    case JsonElement e when e.ValueKind == JsonValueKind.String: return new KV.Str(e.GetString()!);
                    case JsonElement e when e.ValueKind is JsonValueKind.True or JsonValueKind.False: return new KV.Bool(e.GetBoolean());
                    case JsonElement e when e.ValueKind == JsonValueKind.Null: return KV.Null.Instance;
                    default: throw new InvalidOperationException("the adapter cannot describe a " + v.GetValue<object>().GetType().FullName);
                }
            default: throw new InvalidOperationException("unknown node " + n.GetType().Name);
        }
    }

    static string Write(List<JsonNode?> docs) =>
        Host.Library(() =>
        {
            var stream = new YamlStream();
            foreach (var d in docs) stream.Add(new YamlDocument(d == null ? new YamlScalarNode("null") : d.ToYamlNode()));
            return YamlSerializer.Serialize(stream, null);
        });

    static Outcome Patch(string text, Request r, Decoded decoded)
    {
        var (docs, failure) = Load(text);
        if (failure != null) return Outcome.Error(Describe(failure), output: r.Input);

        // The bytes of the unchanged document as this library would write them: it has no way to hand back
        // the original text, only a fresh serialisation of the tree.
        byte[] Original() => Encoding.UTF8.GetBytes(Write(docs));

        int index = r.Args.TryGetProperty("document", out var di) ? di.GetInt32() : 0;
        if (index < 0 || index >= docs.Count)
            return Outcome.Error(new Failure("patch-path", "the document does not exist"), output: r.Input).Emulating("error");

        JsonPatch patch;
        try { patch = Host.Library(() => JsonSerializer.Deserialize<JsonPatch>(r.Args.GetProperty("patch").GetRawText())!); }
        catch (Exception e) { return Outcome.Error(Describe(e), output: Original()); }

        // JsonPatch.Apply works on a copy of the node it is given and returns the new tree.
        var result = Host.Library(() => patch.Apply(docs[index]));
        if (!result.IsSuccess)
        {
            var failing = result.Operation >= 0 && result.Operation < patch.Operations.Count ? patch.Operations[result.Operation] : null;
            var category = failing?.Op == OperationType.Test ? "patch-test" : "patch-path";
            return Outcome.Error(new Failure(category, result.Error ?? "patch failed"), output: Original()).Emulating("classification");
        }
        var after = new List<JsonNode?>(docs) { [index] = result.Result };
        return Outcome.OutputOk(Encoding.UTF8.GetBytes(Write(after)));
    }
}
