#if ONEYAML_LINKED
using OneYaml;
using OneYamlKit.Bench.Models;

namespace OneYamlKit.Bench.Libs;

/// <summary>The metadata the library's source generator writes for the kit's typed models, and for the untyped
/// <c>object</c> read: what a user of the generated path declares once.</summary>
[YamlSerializable(typeof(Workflow))]
[YamlSerializable(typeof(List<Record>))]
[YamlSerializable(typeof(object))]
internal sealed partial class OneYamlContext : YamlSerializerContext
{
}
#endif
