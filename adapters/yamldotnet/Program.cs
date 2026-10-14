using System.Numerics;
using System.Text;
using OneYamlKit.Adapter;
using YamlDotNet.Core;
using YamlDotNet.Core.Events;
using YamlDotNet.Serialization;

return Host.Serve(new YamlDotNetKitAdapter());

/// <summary>YamlDotNet through its public parser (events) and its deserializer into plain objects (values).
/// The library has no edit or patch path, so the adapter declares none.</summary>
sealed class YamlDotNetKitAdapter : IAdapter
{
    public string Name => "yamldotnet";

    public string Version { get; } = PackageVersions.Of("YamlDotNet");

    public string[] Capabilities => ["events", "value", "error-category", "error-position"];

    // `decode`: YamlDotNet reads a string or a text reader, so byte order marks, UTF-16/32 and malformed
    // UTF-8 are handled by the adapter before the library sees the text.
    public string[] Emulates => ["decode"];

    public IReadOnlyDictionary<string, string> Info { get; } = new Dictionary<string, string>
    {
        ["events"] = "YamlDotNet.Core.Parser over a StringReader",
        ["value"] = "DeserializerBuilder().WithAttemptingUnquotedStringTypeDeserialization() into object, one call per document",
        ["library stack"] = $"{Host.LibraryStackBytes / (1024 * 1024)} MiB thread",
        ["runtime"] = System.Runtime.InteropServices.RuntimeInformation.FrameworkDescription,
    };

    static readonly IDeserializer Deserializer =
        new DeserializerBuilder().WithAttemptingUnquotedStringTypeDeserialization().Build();

    public Outcome Run(Request r)
    {
        if (r.Profile != "core") return Outcome.Unsupported("profile " + r.Profile);
        if (r.Op != "events" && r.Op != "value") return Outcome.Unsupported("operation " + r.Op);

        var d = InputDecoder.Decode(r.Input);
        if (d.Failure != null) return Outcome.Error(d.Failure, r.Op == "events" ? [] : null).Emulating("decode");
        var o = r.Op == "events" ? Events(d.Text!) : Value(d.Text!);
        return d.Transformed ? o.Emulating("decode") : o;
    }

    static Failure Describe(Exception e)
    {
        // The category comes from the exception type the library chose; its message is never read.
        var category = e switch
        {
            SyntaxErrorException or SemanticErrorException => "syntax",
            _ => "other",
        };
        if (e is YamlException y && !(y.Start.Line == 0 && y.Start.Column == 0))
            return new Failure(category, e.Message, (int)y.Start.Line, (int)y.Start.Column);
        return new Failure(category, e.Message);
    }

    static Outcome Events(string text)
    {
        var lines = new List<string>();
        // Only the library's own steps are inside the try: whatever it throws is its answer, while a
        // failure in the adapter's formatting must not be mistaken for one.
        var failure = Host.Library(() =>
        {
            var parser = new Parser(new StringReader(text));
            while (true)
            {
                try { if (!parser.MoveNext()) return null; }
                catch (Exception e) { return e; }
                Line(parser.Current!, lines);
            }
        });
        return failure == null ? Outcome.EventsOk(lines) : Outcome.Error(Describe(failure), lines);
    }

    static void Line(ParsingEvent e, List<string> lines)
    {
        switch (e)
        {
            case StreamStart: lines.Add("+STR"); break;
            case StreamEnd: lines.Add("-STR"); break;
            case DocumentStart ds: lines.Add(ds.IsImplicit ? "+DOC" : "+DOC ---"); break;
            case DocumentEnd de: lines.Add(de.IsImplicit ? "-DOC" : "-DOC ..."); break;
            case MappingStart ms:
                lines.Add("+MAP" + (ms.Style == MappingStyle.Flow ? " {}" : "") + Props(ms.Anchor, ms.Tag));
                break;
            case MappingEnd: lines.Add("-MAP"); break;
            case SequenceStart ss:
                lines.Add("+SEQ" + (ss.Style == SequenceStyle.Flow ? " []" : "") + Props(ss.Anchor, ss.Tag));
                break;
            case SequenceEnd: lines.Add("-SEQ"); break;
            case Scalar s:
                lines.Add("=VAL" + Props(s.Anchor, s.Tag) + " " + Mark(s.Style) + EventNotation.Escape(s.Value));
                break;
            case AnchorAlias a: lines.Add("=ALI *" + a.Value.Value); break;
            case Comment: break;
            default: throw new InvalidOperationException("unknown event " + e.GetType().Name);
        }
    }

    static string Props(AnchorName anchor, TagName tag)
    {
        var s = new StringBuilder();
        if (!anchor.IsEmpty) s.Append(" &").Append(anchor.Value);
        if (!tag.IsEmpty) s.Append(" <").Append(tag.Value).Append('>');
        return s.ToString();
    }

    static char Mark(ScalarStyle style) => style switch
    {
        ScalarStyle.SingleQuoted => '\'',
        ScalarStyle.DoubleQuoted => '"',
        ScalarStyle.Literal => '|',
        ScalarStyle.Folded => '>',
        _ => ':',
    };

    static Outcome Value(string text)
    {
        var read = Host.Library(() =>
        {
            var list = new List<object?>();
            try
            {
                var parser = new Parser(new StringReader(text));
                parser.Consume<StreamStart>();
                while (parser.Accept<DocumentStart>(out _))
                    list.Add(Deserializer.Deserialize<object>(parser));
                return (list, (Exception?)null);
            }
            catch (Exception e) { return (list, e); }
        });
        if (read.Item2 != null) return Outcome.Error(Describe(read.Item2));
        var docs = read.Item1;

        try
        {
            var json = Host.Convert(() =>
            {
                var budget = new Budget();
                var sb = new StringBuilder("[");
                for (int i = 0; i < docs.Count; i++)
                {
                    if (i > 0) sb.Append(',');
                    sb.Append(Convert(docs[i], budget).ToJson());
                }
                return sb.Append(']').ToString();
            });
            return Outcome.ValueOk(json);
        }
        catch (TooLargeException e) { return Outcome.Unsupported(e.Message); }
    }

    static KV Convert(object? v, Budget b)
    {
        b.Spend();
        switch (v)
        {
            case null: return KV.Null.Instance;
            case bool x: return new KV.Bool(x);
            case string s: return new KV.Str(s);
            case double d: return new KV.Float(d);
            case float f: return new KV.Float(f);
            case decimal m: return new KV.Float((double)m);
            case sbyte or byte or short or ushort or int or uint or long or ulong:
                return new KV.Int(System.Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture)!);
            case BigInteger bi: return KV.Int.Of(bi);
            case System.Collections.IDictionary dict:
                {
                    var pairs = new List<KeyValuePair<KV, KV>>();
                    foreach (System.Collections.DictionaryEntry e in dict)
                        pairs.Add(new(Convert(e.Key, b), Convert(e.Value, b)));
                    return new KV.Map(pairs);
                }
            case System.Collections.IEnumerable seq:
                {
                    var items = new List<KV>();
                    foreach (var x in seq) items.Add(Convert(x, b));
                    return new KV.Seq(items);
                }
            default: throw new InvalidOperationException("the adapter cannot describe a " + v.GetType().FullName);
        }
    }
}
