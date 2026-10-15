// Kit adapter for oneyaml-csharp, built against a checkout named by KIT_ONEYAML_CSHARP_SRC.
//
// Events and values are read from the lossless YamlDocument, because it is the one public view that
// keeps every node's style, tag and anchor and the key types. Patches go through the document's patch engine. The library's internal event reader is not public, so what the document does not carry
// (explicit document markers, %TAG directives) is recovered here and declared as emulated behaviour.
//
using System.Reflection;
using OneYamlKit.Adapter;

return Host.Serve(new OneYamlKitAdapter());

sealed class OneYamlKitAdapter : IAdapter
{
    public string Name => "oneyaml-csharp";

    // The checkout's commit, stamped in by build.sh ("devel" when it is not a git checkout).
    public string Version { get; } = Assembly.GetExecutingAssembly()
        .GetCustomAttributes<AssemblyMetadataAttribute>()
        .FirstOrDefault(a => a.Key == "OneYamlRevision")?.Value ?? "devel";

#if ONEYAML_LINKED
    // The library reads bytes and detects the encoding itself, so nothing is decoded for it. The
    // internal event reader is not public: no event is available before an error, hence no error-events.
    public string[] Capabilities =>
    [
        "events", "value", "dump", "error-category", "error-position",
        "patch:add", "patch:remove", "patch:replace", "patch:move", "patch:copy", "patch:test", "patch:merge",
    ];

    // style: explicit document markers come from a line scan of the decoded text.
    // tag: the full tag is written out here from the handle and suffix the document keeps and the
    //   %TAG directives of the scan.
    public string[] Emulates => ["style", "tag"];
#else
    public string[] Capabilities => [];

    public string[] Emulates => [];
#endif

    public IReadOnlyDictionary<string, string> Info { get; } = new Dictionary<string, string>
    {
        ["package"] = "OneYaml, from a checkout named by KIT_ONEYAML_CSHARP_SRC",
        ["events"] = "events derived from YamlDocument.Parse (kind, style, tag, anchor, alias); document markers and %TAG directives from a line scan",
        ["value"] = "YamlDocument nodes read with GetValue<object>, so key types and tags survive",
        ["dump"] = "YamlSerializer.SerializeToUtf8Bytes over Dictionary<string, object>, List<object>, long, double, default options",
        ["patch"] = "YamlDocument.ApplyPatch (RFC 6902) and ApplyMergePatch (RFC 7396)",
        ["library stack"] = $"{Host.LibraryStackBytes / (1024 * 1024)} MiB thread",
        ["runtime"] = System.Runtime.InteropServices.RuntimeInformation.FrameworkDescription,
#if ONEYAML_LINKED
        ["linked"] = "yes",
#else
        ["linked"] = "no",
#endif
    };

#if ONEYAML_LINKED
    // Only the library's own exception type is a rejection; anything else escaping a library call is a
    // defect and ends the process.
    public bool IsLibraryException(Exception e) => e is OneYaml.YamlException or Decline;

    public Outcome Run(Request request)
    {
        if (request.Profile != "core") return Outcome.Unsupported("profile " + request.Profile);
        return request.Op switch
        {
            "events" => Events.Run(request),
            "value" => Values.Run(request),
            "patch" => Patches.Run(request),
            "dump" => Lib.Dump(request),
            _ => Outcome.Unsupported("operation " + request.Op),
        };
    }
#else
    public bool IsLibraryException(Exception e) => false;

    public Outcome Run(Request request) => Outcome.Unsupported("the adapter was built without KIT_ONEYAML_CSHARP_SRC");
#endif
}
