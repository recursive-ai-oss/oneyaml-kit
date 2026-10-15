#if ONEYAML_LINKED
using System.Text.Json;
using OneYaml;
using OneYamlKit.Adapter;

/// <summary>Patches through the document's own patch engine: <c>ApplyPatch</c> for RFC 6902 (every operation,
/// any number of them, atomic) and <c>ApplyMergePatch</c> for RFC 7396. The patch goes in as the JSON text the
/// case carries. When it fails the library leaves the document unchanged, so the bytes it would now write are
/// the answer's output.</summary>
static class Patches
{
    public static Outcome Run(Request r)
    {
        bool merge = r.Args.TryGetProperty("format", out var f) && f.GetString() == "merge-patch";
        int document = r.Args.TryGetProperty("document", out var di) && di.ValueKind == JsonValueKind.Number ? di.GetInt32() : 0;
        string patch = r.Args.TryGetProperty("patch", out var p) ? p.GetRawText() : "null";

        var (doc, error) = Lib.Parse(r.Input);
        if (error != null) return Outcome.Error(Lib.Describe(error), output: r.Input);
        using var _ = doc!;

        try
        {
            Host.Library<object?>(() =>
            {
                if (merge) doc!.ApplyMergePatch(patch, document);
                else doc!.ApplyPatch(patch, document);
                return null;
            });
            return Outcome.OutputOk(doc!.ToBytes());
        }
        catch (YamlException e) { return Outcome.Error(Lib.Describe(e), output: doc!.ToBytes()); }
    }
}
#endif
