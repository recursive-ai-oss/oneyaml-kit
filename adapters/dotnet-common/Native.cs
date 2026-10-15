using System.Globalization;
using System.Text.Json;

namespace OneYamlKit.Adapter;

/// <summary>The plain .NET data for a kit value, for a library to write: null, bool, long (ulong above
/// that), double, string, <see cref="List{T}"/> of object and <see cref="Dictionary{TKey,TValue}"/> from string to
/// object. An integer is a JSON number with no point or exponent; any other number is a double.</summary>
public static class Native
{
    /// <summary>The value of a <c>dump</c> request.</summary>
    public static object? Of(Request r)
    {
        if (r.Args.ValueKind != JsonValueKind.Object || !r.Args.TryGetProperty("value", out var v))
            throw new NotSupportedException("the request has no value");
        return From(v, 0);
    }

    static object? From(JsonElement e, int depth)
    {
        if (depth > 1000) throw new NotSupportedException("value nesting too deep for the adapter");
        switch (e.ValueKind)
        {
            case JsonValueKind.Null: return null;
            case JsonValueKind.True: return true;
            case JsonValueKind.False: return false;
            case JsonValueKind.String: return e.GetString();
            case JsonValueKind.Number:
                var raw = e.GetRawText();
                if (raw.IndexOfAny(['.', 'e', 'E']) < 0)
                {
                    if (e.TryGetInt64(out var l)) return l;
                    if (e.TryGetUInt64(out var u)) return u;
                    throw new NotSupportedException("integer beyond 64 bits");
                }
                return double.Parse(raw, CultureInfo.InvariantCulture);
            case JsonValueKind.Array:
                var list = new List<object?>();
                foreach (var x in e.EnumerateArray()) list.Add(From(x, depth + 1));
                return list;
            case JsonValueKind.Object:
                if (e.TryGetProperty("$float", out var f) && f.ValueKind == JsonValueKind.String)
                    return f.GetString() switch
                    {
                        "inf" => double.PositiveInfinity,
                        "-inf" => double.NegativeInfinity,
                        _ => double.NaN,
                    };
                if (e.TryGetProperty("$map", out _))
                    throw new NotSupportedException("mapping with keys that are not strings");
                var map = new Dictionary<string, object?>();
                foreach (var p in e.EnumerateObject()) map[p.Name] = From(p.Value, depth + 1);
                return map;
        }
        throw new NotSupportedException("value of an unknown kind");
    }
}
