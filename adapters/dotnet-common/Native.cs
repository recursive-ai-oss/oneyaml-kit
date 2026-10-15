using System.Globalization;
using System.Numerics;
using System.Text.Json;

namespace OneYamlKit.Adapter;

/// <summary>The plain .NET data for a kit value, for a library to write: null, bool, long (ulong above
/// that), double, string, <see cref="List{T}"/> of object and <see cref="Dictionary{TKey,TValue}"/> from string to
/// object. An integer is a JSON number with no point or exponent; any other number is a double. A byte
/// string (a node tagged <c>tag:yaml.org,2002:binary</c> that holds base64) is whatever the adapter's
/// <c>binary</c> function makes of the bytes, and declined (with <c>noBinary</c> as the reason) when there is none.
/// An integer beyond 64 bits and a mapping with keys that are not strings are an adapter gap unless the adapter passes
/// <c>big</c> (the type that holds the integer) or <c>pairs</c> (the mapping type built from the key/value pairs).</summary>
public static class Native
{
    /// <summary>The value of a <c>dump</c> request.</summary>
    public const string BinaryTag = "tag:yaml.org,2002:binary";

    public static object? Of(Request r, Func<byte[], object?>? binary = null, string? noBinary = null,
        Func<BigInteger, object?>? big = null, Func<List<KeyValuePair<object?, object?>>, object?>? pairs = null)
    {
        if (r.Args.ValueKind != JsonValueKind.Object || !r.Args.TryGetProperty("value", out var v))
            throw new NotSupportedException("the request has no value");
        return From(v, 0, binary, noBinary, big, pairs);
    }

    static object? From(JsonElement e, int depth, Func<byte[], object?>? binary, string? noBinary,
        Func<BigInteger, object?>? big, Func<List<KeyValuePair<object?, object?>>, object?>? pairs)
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
                    if (big != null) return big(BigInteger.Parse(raw, CultureInfo.InvariantCulture));
                    throw new NotSupportedException("the adapter does not yet pass an integer beyond 64 bits to the library");
                }
                return double.Parse(raw, CultureInfo.InvariantCulture);
            case JsonValueKind.Array:
                var list = new List<object?>();
                foreach (var x in e.EnumerateArray()) list.Add(From(x, depth + 1, binary, noBinary, big, pairs));
                return list;
            case JsonValueKind.Object:
                if (e.TryGetProperty("$float", out var f) && f.ValueKind == JsonValueKind.String)
                    return f.GetString() switch
                    {
                        "inf" => double.PositiveInfinity,
                        "-inf" => double.NegativeInfinity,
                        _ => double.NaN,
                    };
                if (e.TryGetProperty("$tagged", out var tagged) && tagged.ValueKind == JsonValueKind.Object)
                {
                    if (!tagged.TryGetProperty("tag", out var tag) || tag.GetString() != BinaryTag
                        || !tagged.TryGetProperty("value", out var text) || text.ValueKind != JsonValueKind.String)
                        throw new NotSupportedException("tagged value");
                    if (binary == null)
                        throw new NotSupportedException(noBinary ?? "the library has no byte string it writes as !!binary");
                    return binary(Convert.FromBase64String(text.GetString()!));
                }
                if (e.TryGetProperty("$map", out var entries))
                {
                    if (pairs == null || entries.ValueKind != JsonValueKind.Array) throw new NotSupportedException("the adapter does not yet pass a mapping with keys that are not strings to the library");
                    var list2 = new List<KeyValuePair<object?, object?>>();
                    foreach (var kv in entries.EnumerateArray())
                    {
                        if (kv.ValueKind != JsonValueKind.Array || kv.GetArrayLength() != 2)
                            throw new NotSupportedException("a $map entry that is not a [key, value] pair");
                        list2.Add(new(From(kv[0], depth + 1, binary, noBinary, big, pairs), From(kv[1], depth + 1, binary, noBinary, big, pairs)));
                    }
                    return pairs(list2);
                }
                var map = new Dictionary<string, object?>();
                foreach (var p in e.EnumerateObject()) map[p.Name] = From(p.Value, depth + 1, binary, noBinary, big, pairs);
                return map;
        }
        throw new NotSupportedException("value of an unknown kind");
    }
}
