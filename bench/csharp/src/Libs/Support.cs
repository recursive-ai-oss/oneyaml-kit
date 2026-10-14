using System.Globalization;
using System.Numerics;
using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace OneYamlKit.Bench.Libs;

public static class Support
{
    public static readonly UTF8Encoding Utf8 = new(false);

    /// <summary>The exact version of a NuGet package, from the dependency file of the built program
    /// (the pinned restore produced it), so the figure comes from the lock and not from the source.</summary>
    public static string Version(string package)
    {
        try
        {
            var name = Assembly.GetEntryAssembly()!.GetName().Name;
            using var doc = JsonDocument.Parse(File.ReadAllBytes(Path.Combine(AppContext.BaseDirectory, name + ".deps.json")));
            foreach (var lib in doc.RootElement.GetProperty("libraries").EnumerateObject())
            {
                int slash = lib.Name.IndexOf('/');
                if (slash > 0 && string.Equals(lib.Name[..slash], package, StringComparison.OrdinalIgnoreCase))
                    return lib.Name[(slash + 1)..];
            }
        }
        catch (Exception) { /* fall through */ }
        return "unknown";
    }

    public static Unsupported No(string why) => new(why);

    // ---- System.Text.Json values -----------------------------------------------------------------

    public static J FromJsonElement(JsonElement e)
    {
        switch (e.ValueKind)
        {
            case JsonValueKind.Null: return J.Null.I;
            case JsonValueKind.True: return new J.Bool(true);
            case JsonValueKind.False: return new J.Bool(false);
            case JsonValueKind.String: return new J.Str(e.GetString()!);
            case JsonValueKind.Number:
                {
                    var raw = e.GetRawText();
                    return raw.IndexOfAny(['.', 'e', 'E']) >= 0
                        ? new J.Float(double.Parse(raw, CultureInfo.InvariantCulture))
                        : new J.Int(BigInteger.Parse(raw, CultureInfo.InvariantCulture));
                }
            case JsonValueKind.Array:
                return new J.Arr(e.EnumerateArray().Select(FromJsonElement).ToList());
            case JsonValueKind.Object:
                {
                    var o = new J.Obj();
                    foreach (var p in e.EnumerateObject()) o.Put(p.Name, FromJsonElement(p.Value));
                    return o;
                }
            default: throw new InvalidOperationException("not a JSON value");
        }
    }

    /// <summary>A JsonNode as a common value. Yaml2JsonNode keeps every number as a decimal, so a number
    /// with a fractional scale is a float and one without is an integer, which is all the information the
    /// node retains.</summary>
    public static J FromJsonNode(JsonNode? n)
    {
        switch (n)
        {
            case null: return J.Null.I;
            case JsonObject o:
                {
                    var r = new J.Obj();
                    foreach (var kv in o) r.Put(kv.Key, FromJsonNode(kv.Value));
                    return r;
                }
            case JsonArray a: return new J.Arr(a.Select(FromJsonNode).ToList());
            case JsonValue v:
                switch (v.GetValue<object>())
                {
                    case string s: return new J.Str(s);
                    case bool b: return new J.Bool(b);
                    case decimal m:
                        return (decimal.GetBits(m)[3] >> 16 & 0xFF) == 0
                            ? new J.Int(new BigInteger(m))
                            : new J.Float((double)m);
                    case double d: return new J.Float(d);
                    case long or int or short or byte or sbyte or ulong or uint or ushort:
                        return new J.Int(new BigInteger(Convert.ToDecimal(v.GetValue<object>(), CultureInfo.InvariantCulture)));
                    case JsonElement e: return FromJsonElement(e);
                    default: throw new InvalidOperationException("unexpected node value " + v.GetValue<object>().GetType().FullName);
                }
            default: throw new InvalidOperationException("unknown node");
        }
    }

    public static JsonNode? ToJsonNode(J v) => v switch
    {
        J.Null => null,
        J.Bool b => JsonValue.Create(b.V),
        J.Int i => JsonNode.Parse(i.V.ToString(CultureInfo.InvariantCulture)),
        J.Float f => JsonValue.Create(f.V),
        J.Str s => JsonValue.Create(s.V),
        J.Arr a => new JsonArray(a.V.Select(ToJsonNode).ToArray()),
        J.Obj o => ToObject(o),
        _ => throw new InvalidOperationException("unknown value"),
    };

    static JsonObject ToObject(J.Obj o)
    {
        var r = new JsonObject();
        foreach (var (k, x) in o.V) r[k] = ToJsonNode(x);
        return r;
    }
}
