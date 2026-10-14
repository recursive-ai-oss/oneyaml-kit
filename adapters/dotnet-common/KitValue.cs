using System.Globalization;
using System.Numerics;
using System.Text;

namespace OneYamlKit.Adapter;

/// <summary>A kit value: the JSON-comparable form of a YAML document (see the case format).</summary>
public abstract class KV
{
    public sealed class Null : KV { public static readonly Null Instance = new(); }
    public sealed class Bool(bool value) : KV { public bool Value { get; } = value; }
    public sealed class Str(string value) : KV { public string Value { get; } = value; }
    /// <summary>An integer, kept as digits so that values beyond 64 bits survive.</summary>
    public sealed class Int(string digits) : KV
    {
        public string Digits { get; } = digits;
        public static Int Of(BigInteger v) => new(v.ToString(CultureInfo.InvariantCulture));
    }
    public sealed class Float(double value) : KV { public double Value { get; } = value; }
    public sealed class Seq(List<KV> items) : KV { public List<KV> Items { get; } = items; }
    public sealed class Map(List<KeyValuePair<KV, KV>> pairs) : KV { public List<KeyValuePair<KV, KV>> Pairs { get; } = pairs; }
    public sealed class Tagged(string tag, KV value) : KV { public string Tag { get; } = tag; public KV Value { get; } = value; }

    public string ToJson()
    {
        var sb = new StringBuilder();
        Write(this, sb);
        return sb.ToString();
    }

    static void Write(KV v, StringBuilder o)
    {
        switch (v)
        {
            case Null: o.Append("null"); break;
            case Bool b: o.Append(b.Value ? "true" : "false"); break;
            case Str s: JsonString(o, s.Value); break;
            case Int i: o.Append(i.Digits); break;
            case Float f: WriteFloat(f.Value, o); break;
            case Seq q:
                o.Append('[');
                for (int k = 0; k < q.Items.Count; k++)
                {
                    if (k > 0) o.Append(',');
                    Write(q.Items[k], o);
                }
                o.Append(']');
                break;
            case Tagged t:
                o.Append("{\"$tagged\":{\"tag\":");
                JsonString(o, t.Tag);
                o.Append(",\"value\":");
                Write(t.Value, o);
                o.Append("}}");
                break;
            case Map m:
                // Keys starting with `$` are reserved for the kit's escape forms, so a mapping that
                // has one, or any key that is not a string, is written in the `$map` form.
                var plain = m.Pairs.All(p => p.Key is Str s && !s.Value.StartsWith('$'));
                if (plain)
                {
                    var seen = new HashSet<string>(StringComparer.Ordinal);
                    plain = m.Pairs.All(p => seen.Add(((Str)p.Key).Value));
                }
                if (plain)
                {
                    o.Append('{');
                    for (int k = 0; k < m.Pairs.Count; k++)
                    {
                        if (k > 0) o.Append(',');
                        JsonString(o, ((Str)m.Pairs[k].Key).Value);
                        o.Append(':');
                        Write(m.Pairs[k].Value, o);
                    }
                    o.Append('}');
                }
                else
                {
                    o.Append("{\"$map\":[");
                    for (int k = 0; k < m.Pairs.Count; k++)
                    {
                        if (k > 0) o.Append(',');
                        o.Append('[');
                        Write(m.Pairs[k].Key, o);
                        o.Append(',');
                        Write(m.Pairs[k].Value, o);
                        o.Append(']');
                    }
                    o.Append("]}");
                }
                break;
            default: throw new InvalidOperationException("unknown kit value");
        }
    }

    static void WriteFloat(double d, StringBuilder o)
    {
        if (double.IsNaN(d)) o.Append("{\"$float\":\"nan\"}");
        else if (double.IsPositiveInfinity(d)) o.Append("{\"$float\":\"inf\"}");
        else if (double.IsNegativeInfinity(d)) o.Append("{\"$float\":\"-inf\"}");
        else
        {
            var s = d.ToString("R", CultureInfo.InvariantCulture);
            o.Append(s);
            // A float must carry a decimal point or an exponent: 1 and 1.0 are different values.
            if (s.IndexOfAny(['.', 'E', 'e']) < 0) o.Append(".0");
        }
    }

    public static void JsonString(StringBuilder o, string s)
    {
        o.Append('"');
        foreach (var c in s)
        {
            switch (c)
            {
                case '"': o.Append("\\\""); break;
                case '\\': o.Append("\\\\"); break;
                case '\n': o.Append("\\n"); break;
                case '\r': o.Append("\\r"); break;
                case '\t': o.Append("\\t"); break;
                default:
                    if (c < 0x20) o.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                    else o.Append(c);
                    break;
            }
        }
        o.Append('"');
    }
}

/// <summary>Raised when a result is too large to describe as a kit value (an alias bomb that the
/// library kept as a shared graph, for instance).</summary>
public sealed class TooLargeException(string message) : Exception(message);

/// <summary>Counts the nodes a conversion creates and stops it past a budget.</summary>
public sealed class Budget(long limit = 4_000_000)
{
    long _used;
    public void Spend()
    {
        if (++_used > limit)
            throw new TooLargeException($"the result expands to more than {limit} nodes; the adapter does not render it");
    }
}

/// <summary>The event notation of the yaml-test-suite (see the case format).</summary>
public static class EventNotation
{
    public static string Escape(string value)
    {
        var sb = new StringBuilder(value.Length);
        foreach (var c in value)
        {
            switch (c)
            {
                case '\\': sb.Append("\\\\"); break;
                case '\0': sb.Append("\\0"); break;
                case '\b': sb.Append("\\b"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default: sb.Append(c); break;
            }
        }
        return sb.ToString();
    }

    public static char ScalarMark(string style) => style switch
    {
        "plain" => ':',
        "single" => '\'',
        "double" => '"',
        "literal" => '|',
        "folded" => '>',
        _ => throw new ArgumentException("unknown scalar style " + style),
    };
}
