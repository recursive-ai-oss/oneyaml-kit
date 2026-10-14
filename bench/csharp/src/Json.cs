using System.Globalization;
using System.Numerics;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;

namespace OneYamlKit.Bench;

/// <summary>The common form of a YAML value, the same one the other harnesses compare: an
/// integer is not the float with the same digits, and "1" is not 1. Mappings are unordered.</summary>
public abstract class J
{
    public sealed class Null : J { public static readonly Null I = new(); }
    public sealed class Bool(bool v) : J { public bool V { get; } = v; }
    public sealed class Int(BigInteger v) : J { public BigInteger V { get; } = v; }
    public sealed class Float(double v) : J { public double V { get; } = v; }
    public sealed class Str(string v) : J { public string V { get; } = v; }
    public sealed class Arr(List<J> v) : J { public List<J> V { get; } = v; }

    /// <summary>A mapping keyed by the key's scalar text (see <see cref="KeyText"/>). A duplicate key keeps the last value.</summary>
    public sealed class Obj : J
    {
        public Dictionary<string, J> V { get; } = new(StringComparer.Ordinal);
        public void Put(string k, J v) => V[k] = v;
    }

    /// <summary>Text for messages: quotes and control characters escaped, everything else as it is.</summary>
    public static readonly JsonSerializerOptions Text = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    public static J Of(long n) => new Int(n);
    public static J Of(string s) => new Str(s);
    public static J Of(bool b) => new Bool(b);

    /// <summary>The text a mapping key is compared by: a string as it is, anything else as its JSON form.</summary>
    public static string KeyText(J k) => k is Str s ? s.V : k.ToString();

    // ---- reading JSON ------------------------------------------------------------------

    public static J Parse(byte[] json)
    {
        using var doc = JsonDocument.Parse(json, new JsonDocumentOptions { MaxDepth = 4096 });
        return From(doc.RootElement);
    }

    static J From(JsonElement e)
    {
        switch (e.ValueKind)
        {
            case JsonValueKind.Null: return Null.I;
            case JsonValueKind.True: return new Bool(true);
            case JsonValueKind.False: return new Bool(false);
            case JsonValueKind.String: return new Str(e.GetString()!);
            case JsonValueKind.Number:
                {
                    var raw = e.GetRawText();
                    // A decimal point or an exponent makes a number a float, as the other harnesses read it.
                    if (raw.IndexOfAny(['.', 'e', 'E']) >= 0)
                        return new Float(double.Parse(raw, CultureInfo.InvariantCulture));
                    return new Int(BigInteger.Parse(raw, CultureInfo.InvariantCulture));
                }
            case JsonValueKind.Array:
                {
                    var l = new List<J>();
                    foreach (var x in e.EnumerateArray()) l.Add(From(x));
                    return new Arr(l);
                }
            case JsonValueKind.Object:
                {
                    var o = new Obj();
                    foreach (var p in e.EnumerateObject()) o.Put(p.Name, From(p.Value));
                    return o;
                }
            default: throw new InvalidOperationException("not JSON");
        }
    }

    // ---- writing, for messages ---------------------------------------------------------

    /// <summary>The value as JSON text, clipped: this is for messages, which are clipped anyway.</summary>
    public override string ToString()
    {
        var sb = new StringBuilder();
        Write(this, sb, 0, 400);
        return sb.ToString();
    }

    /// <summary>The whole value as JSON text.</summary>
    public string ToJson()
    {
        var sb = new StringBuilder();
        Write(this, sb, 0, int.MaxValue);
        return sb.ToString();
    }

    static void Write(J v, StringBuilder sb, int depth, int limit)
    {
        if (depth > 4000 || sb.Length > limit) { sb.Append('…'); return; }
        switch (v)
        {
            case Null: sb.Append("null"); break;
            case Bool b: sb.Append(b.V ? "true" : "false"); break;
            case Int i: sb.Append(i.V.ToString(CultureInfo.InvariantCulture)); break;
            case Float f:
                if (double.IsNaN(f.V)) sb.Append("NaN");
                else if (double.IsInfinity(f.V)) sb.Append(f.V > 0 ? "Infinity" : "-Infinity");
                else
                {
                    var s = f.V.ToString("R", CultureInfo.InvariantCulture);
                    sb.Append(s);
                    if (s.IndexOfAny(['.', 'E', 'e']) < 0) sb.Append(".0");
                }
                break;
            case Str s: sb.Append(JsonSerializer.Serialize(s.V, Text)); break;
            case Arr a:
                sb.Append('[');
                for (int k = 0; k < a.V.Count; k++) { if (k > 0) sb.Append(','); Write(a.V[k], sb, depth + 1, limit); }
                sb.Append(']');
                break;
            case Obj o:
                sb.Append('{');
                bool first = true;
                foreach (var kv in o.V)
                {
                    if (!first) sb.Append(',');
                    first = false;
                    sb.Append(JsonSerializer.Serialize(kv.Key, Text)).Append(':');
                    Write(kv.Value, sb, depth + 1, limit);
                }
                sb.Append('}');
                break;
        }
    }

    // ---- comparison --------------------------------------------------------------------

    /// <summary>Where two values first differ, as a pointer and the two sides; null when equal. The harness
    /// runs on a thread with a large stack, so a deeply nested value is no problem.</summary>
    public static string? FirstDiff(J got, J want, string at = "")
    {
        switch (got, want)
        {
            case (Obj a, Obj b):
                foreach (var (k, bv) in b.V)
                {
                    var p = $"{at}/{k.Replace("~", "~0").Replace("/", "~1")}";
                    if (!a.V.TryGetValue(k, out var av)) return $"missing {p}";
                    var d = FirstDiff(av, bv, p);
                    if (d != null) return d;
                }
                foreach (var k in a.V.Keys)
                    if (!b.V.ContainsKey(k)) return $"unexpected {at}/{k}";
                return null;
            case (Arr a, Arr b):
                for (int i = 0; i < Math.Min(a.V.Count, b.V.Count); i++)
                {
                    var d = FirstDiff(a.V[i], b.V[i], $"{at}/{i}");
                    if (d != null) return d;
                }
                return a.V.Count != b.V.Count ? $"{at}: {a.V.Count} items, expected {b.V.Count}" : null;
            default:
                return Equal(got, want) ? null
                    : $"at {(at.Length == 0 ? "/" : at)}: got {Clip(got.ToString())}, expected {Clip(want.ToString())}";
        }
    }

    static bool Equal(J a, J b) => (a, b) switch
    {
        (Null, Null) => true,
        (Bool x, Bool y) => x.V == y.V,
        (Int x, Int y) => x.V == y.V,
        (Float x, Float y) => x.V.Equals(y.V),
        (Str x, Str y) => string.Equals(x.V, y.V, StringComparison.Ordinal),
        _ => false,
    };

    public static string Clip(string s, int n = 80)
    {
        var e = StringInfo.GetTextElementEnumerator(s);
        int c = 0, end = s.Length;
        while (e.MoveNext())
        {
            if (++c > n) { end = e.ElementIndex; return s[..end] + "…"; }
        }
        return s;
    }
}
