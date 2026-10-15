#if ONEYAML_LINKED
using OneYaml;
using OneYamlKit.Adapter;

/// <summary>The adapter cannot describe this input (it is listed in the deviations); not a verdict on the library.</summary>
sealed class Decline(string reason) : Exception(reason);

/// <summary>Shared by the three operations: parsing, and the library's errors in the kit's terms.</summary>
static class Lib
{
    /// <summary>Parses the input as the library would be given it, bytes and all (it detects UTF-16/32 and
    /// byte order marks itself). Returns the document, or the library's error.</summary>
    public static (YamlDocument? Doc, YamlException? Error) Parse(byte[] input)
    {
        try { return (Host.Library(() => YamlDocument.Parse(input)), null); }
        catch (YamlException e) { return (null, e); }
    }

    /// <summary>The kit's category for the library's.</summary>
    public static string Category(YamlErrorCategory c) => c switch
    {
        YamlErrorCategory.Syntax => "syntax",
        YamlErrorCategory.Encoding => "encoding",
        YamlErrorCategory.Limit => "limit",
        YamlErrorCategory.UnresolvedTag => "unresolved-tag",
        YamlErrorCategory.PatchPath => "patch-path",
        YamlErrorCategory.PatchTest => "patch-test",
        YamlErrorCategory.PatchInvalid => "patch-invalid",
        YamlErrorCategory.RefusedAlias => "refused-alias",
        YamlErrorCategory.RefusedAmbiguous => "refused-ambiguous",
        YamlErrorCategory.DuplicateKey => "duplicate-key",
        // A binding error is about a .NET type; the kit has no name for it.
        _ => "other",
    };

    public static Failure Describe(YamlException e) =>
        e.Line > 0
            ? new Failure(Category(e.Category), e.Message, e.Line, e.Column > 0 ? e.Column : null)
            : new Failure(Category(e.Category), e.Message);
}
#endif
