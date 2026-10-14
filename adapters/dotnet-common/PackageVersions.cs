using System.Reflection;
using System.Text.Json;

namespace OneYamlKit.Adapter;

/// <summary>Reads the exact version of a NuGet package from the built program's dependency file. Some
/// libraries stamp their assemblies with a version unrelated to the package, so the dependency file,
/// which is what the pinned restore produced, is the reliable source.</summary>
public static class PackageVersions
{
    public static string Of(string package)
    {
        try
        {
            var name = Assembly.GetEntryAssembly()!.GetName().Name;
            using var doc = JsonDocument.Parse(File.ReadAllBytes(Path.Combine(AppContext.BaseDirectory, name + ".deps.json")));
            foreach (var lib in doc.RootElement.GetProperty("libraries").EnumerateObject())
            {
                var key = lib.Name;
                int slash = key.IndexOf('/');
                if (slash > 0 && string.Equals(key[..slash], package, StringComparison.OrdinalIgnoreCase)) return key[(slash + 1)..];
            }
        }
        catch (Exception) { /* fall through */ }
        return "unknown";
    }
}
