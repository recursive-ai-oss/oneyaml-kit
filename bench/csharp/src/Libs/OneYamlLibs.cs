#if ONEYAML_LINKED
using System.Reflection;

namespace OneYamlKit.Bench.Libs;

/// <summary>oneyaml-csharp, built from the checkout named by KIT_ONEYAML_CSHARP_SRC. It takes part in no task
/// yet: every runner is refused until the library has the entry point for it. Each task is added here with the
/// library's own call for it.
///
/// The library has two ways to bind typed data, and they are separate rows so each is compared with the
/// strongest competitor on its own footing: <c>oneyaml-csharp</c> is the reflection binder (what a user gets
/// with no setup), <c>oneyaml-csharp-gen</c> the source-generated one. Untyped and document tasks do not
/// involve a binder, so when they are added the two rows run the same code and the generated row may leave
/// them out.</summary>
public abstract class OneYamlLibBase : ILibrary
{
    public abstract string Id { get; }
    public string Package => "OneYaml";

    // The checkout's commit, which the build stamps into the harness.
    public string Version { get; } = typeof(OneYamlLibBase).Assembly
        .GetCustomAttributes<AssemblyMetadataAttribute>()
        .FirstOrDefault(a => a.Key == "OneYamlRevision")?.Value ?? "devel";

    public Role Role => Role.Own;
    public abstract IReadOnlyDictionary<string, string> Settings { get; }

    // Entry points to wire, per task (names provisional):
    //   typed-load     YamlSerializer.Deserialize<T> over UTF-8 bytes (gen: with a YamlSerializerContext)
    //   generic-load   YamlSerializer.Deserialize<object>
    //   dump           YamlSerializer.Serialize
    //   document-parse, unchanged-emit, edit-script   YamlDocument parse, write and indexer or Set edits
    //   patch          YamlDocument.ApplyPatch
    public Runner Runner(BenchTask task, Workload w) => throw Support.No("not written yet");
}

public sealed class OneYamlReflectionLib : OneYamlLibBase
{
    public override string Id => "oneyaml-csharp";

    public override IReadOnlyDictionary<string, string> Settings { get; } = new Dictionary<string, string>
    {
        ["binding"] = "reflection",
    };
}

public sealed class OneYamlGeneratedLib : OneYamlLibBase
{
    public override string Id => "oneyaml-csharp-gen";

    public override IReadOnlyDictionary<string, string> Settings { get; } = new Dictionary<string, string>
    {
        ["binding"] = "source-generated",
    };
}
#endif
