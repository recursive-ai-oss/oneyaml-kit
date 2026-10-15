#if ONEYAML_LINKED
using System.Globalization;
using System.Numerics;
using System.Text.Json;
using OneYaml;
using OneYamlKit.Adapter;

/// <summary>Patches through the document's pointer methods. A patch of one add, remove or replace maps onto
/// <c>Add</c>, <c>Remove</c> and <c>Set</c>. The library applies a whole patch atomically with
/// <c>ApplyPatch</c> and a merge patch with <c>ApplyMergePatch</c>; until those are wired, multi-operation
/// patches, test, move, copy and merge patches are not answered (the seams are marked below).</summary>
static class Patches
{
    public static Outcome Run(Request r)
    {
        // Seam: YamlDocument.ApplyMergePatch(...) answers format "merge-patch" (capability patch:merge).
        if (r.Args.TryGetProperty("format", out var f) && f.GetString() != "json-patch")
            return Outcome.Unsupported("merge patches wait for ApplyMergePatch");
        int document = r.Args.TryGetProperty("document", out var di) && di.ValueKind == JsonValueKind.Number ? di.GetInt32() : 0;
        if (!r.Args.TryGetProperty("patch", out var ops) || ops.ValueKind != JsonValueKind.Array)
            return Refuse("patch-invalid", "a patch is an array of operations", r.Input);

        var (doc, error) = Lib.Parse(r.Input);
        if (error != null) return Outcome.Error(Lib.Describe(error), output: r.Input);
        using var _ = doc!;

        int n = ops.GetArrayLength();
        // An empty patch asks for the document back, byte for byte.
        if (n == 0) return Outcome.OutputOk(doc!.ToBytes());
        // Seam: YamlDocument.ApplyPatch(patch) takes any number of operations, test, move and copy included,
        // and leaves the document unchanged when one fails. Until then only a single operation is answered.
        if (n != 1) return Outcome.Unsupported("multi-operation patches wait for ApplyPatch");

        var op = ops[0];
        string? name = op.ValueKind == JsonValueKind.Object && op.TryGetProperty("op", out var nm) && nm.ValueKind == JsonValueKind.String ? nm.GetString() : null;
        if (name is not ("add" or "remove" or "replace"))
        {
            if (name is "test" or "move" or "copy") return Outcome.Unsupported($"{name} waits for ApplyPatch");
            return Refuse("patch-invalid", $"operation {name ?? "(none)"} is not an RFC 6902 operation", doc!.ToBytes());
        }
        if (!op.TryGetProperty("path", out var p) || p.ValueKind != JsonValueKind.String)
            return Refuse("patch-invalid", "an operation needs a path", doc!.ToBytes());
        string path = p.GetString()!;
        bool hasValue = op.TryGetProperty("value", out var v);
        if (name != "remove" && !hasValue)
            return Refuse("patch-invalid", name + " needs a value", doc!.ToBytes());

        try
        {
            Host.Library<object?>(() =>
            {
                switch (name)
                {
                    case "add": doc!.Add(path, ToObject(v), document); break;
                    case "remove": doc!.Remove(path, document); break;
                    default:
                        // replace: the target must exist (Set would create it); GetNode raises the library's own
                        // path error when it does not.
                        doc!.GetNode(path, document);
                        doc.Set(path, ToObject(v), document);
                        break;
                }
                return null;
            });
            return Outcome.OutputOk(doc!.ToBytes());
        }
        catch (YamlException e) { return Outcome.Error(Lib.Describe(e), output: doc!.ToBytes()); }
    }

    // An answer the adapter raises itself, because the request is not a patch the library is asked about.
    static Outcome Refuse(string category, string message, byte[] unchanged) =>
        Outcome.Error(new Failure(category, message), output: unchanged).Emulating("error");

    /// <summary>A JSON value as the plain objects the document's setters take.</summary>
    static object? ToObject(JsonElement e)
    {
        switch (e.ValueKind)
        {
            case JsonValueKind.True: return true;
            case JsonValueKind.False: return false;
            case JsonValueKind.String: return e.GetString();
            case JsonValueKind.Number:
                {
                    string raw = e.GetRawText();
                    if (raw.IndexOfAny(['.', 'e', 'E']) >= 0) return double.Parse(raw, CultureInfo.InvariantCulture);
                    if (long.TryParse(raw, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out long l)) return l;
                    return BigInteger.Parse(raw, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture);
                }
            case JsonValueKind.Array:
                return e.EnumerateArray().Select(ToObject).ToList();
            case JsonValueKind.Object:
                {
                    var d = new Dictionary<string, object?>();
                    foreach (var prop in e.EnumerateObject()) d[prop.Name] = ToObject(prop.Value);
                    return d;
                }
            default: return null;
        }
    }
}
#endif
