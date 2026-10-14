// Kit adapter for oneyaml-csharp, built against a checkout named by KIT_ONEYAML_CSHARP_SRC.
//
// It declares no capabilities yet, so the kit sends it no case and counts every case as
// unsupported. Each operation below is a seam: a method that says what it will call once the
// library offers it. Declaring a capability and filling its method go together, and nowhere
// else changes.
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

    public string[] Capabilities => [];

    public string[] Emulates => [];

    public IReadOnlyDictionary<string, string> Info { get; } = new Dictionary<string, string>
    {
        ["package"] = "OneYaml, from a checkout named by KIT_ONEYAML_CSHARP_SRC",
        ["source"] = "no operation is wired yet",
#if ONEYAML_LINKED
        ["linked"] = "yes",
#else
        ["linked"] = "no",
#endif
    };

    // Only the library's own exception hierarchy (YamlException and its subclasses) is a
    // rejection; anything else escaping a library call is a defect and ends the process.
    // Once the library has that type: e is OneYaml.YamlException.
    public bool IsLibraryException(Exception e) => false;

    public Outcome Run(Request request) => request.Op switch
    {
        "events" => Events(request),
        "value" => Value(request),
        "patch" => Patch(request),
        _ => Outcome.Unsupported("operation " + request.Op),
    };

    // events will run the library's event reader over every document and answer with the kit's
    // event lines, or the library's error with its category and position. Declare "events",
    // "error-events", "error-category" and "error-position" with it.
    // Calls: YamlDocument / the pull event reader (names provisional).
    static Outcome Events(Request request) => Outcome.Unsupported("events: not wired yet");

    // value will load every document into generic values and convert them with KitValue, or
    // answer the library's error. Declare "value" with it.
    // Calls: YamlSerializer.Deserialize<object> (name provisional).
    static Outcome Value(Request request) => Outcome.Unsupported("value: not wired yet");

    // patch will parse the input as a lossless document, apply the patch the case names (RFC
    // 6902, RFC 7396 or the edit forms) and answer the whole stream, or the library's refusal.
    // Declare the "patch:<op>" capabilities it supports.
    // Calls: YamlDocument.ApplyPatch (name provisional).
    static Outcome Patch(Request request) => Outcome.Unsupported("patch: not wired yet");
}
