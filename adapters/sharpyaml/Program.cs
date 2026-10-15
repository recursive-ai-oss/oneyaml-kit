using System.Numerics;
using System.Text;
using OneYamlKit.Adapter;
using SharpYaml;
using SharpYaml.Events;
using SharpYaml.Model;

return Host.Serve(new SharpYamlKitAdapter());

/// <summary>SharpYaml through its event parser (events), its YamlStream model (values) and its
/// YamlSyntaxTree (edits). The syntax tree only replaces a span of source text, so the edit path
/// needs the adapter to find the span and write the replacement; see Edit.cs.</summary>
sealed class SharpYamlKitAdapter : IAdapter
{
    public string Name => "sharpyaml";

    public string Version { get; } = PackageVersions.Of("SharpYaml");

    public string[] Capabilities => ["events", "value", "patch:replace", "error-category", "error-position", "error-events"];

    // Only SharpYaml's own exception hierarchy is a rejection; anything else escaping the library is a
    // defect, and the process ends so the kit records a crash.
    public bool IsLibraryException(Exception e) => e is YamlException;

    // decode: SharpYaml reads a string, so byte order marks, UTF-16/32 and malformed UTF-8 are the adapter's.
    // error: errors for a missing path, a malformed pointer or an unknown operation come from the
    //   adapter; the library has no patch or pointer concept.
    // unchanged: the adapter reports the original bytes when any step of a patch fails.
    // pointer: the adapter resolves the JSON Pointer to a source span from the library's parse events.
    // source-text: the adapter writes the YAML text that replaces the span (style, quoting, block header).
    public string[] Emulates => ["decode", "error", "unchanged", "pointer", "source-text"];

    public IReadOnlyDictionary<string, string> Info { get; } = new Dictionary<string, string>
    {
        ["events"] = "SharpYaml.Parser.CreateParser over a StringReader",
        ["value"] = "SharpYaml.Model.YamlStream.Load, then ToObject<object>() per document, default options",
        ["patch"] = "YamlSyntaxTree.Parse(...).WithTextChange(start, length, rawYaml) for replace of a scalar",
        ["library stack"] = $"{Host.LibraryStackBytes / (1024 * 1024)} MiB thread",
        ["runtime"] = System.Runtime.InteropServices.RuntimeInformation.FrameworkDescription,
    };

    public Outcome Run(Request r)
    {
        if (r.Profile != "core") return Outcome.Unsupported("profile " + r.Profile);
        if (r.Op == "patch") return Edit.Patch(r);
        if (r.Op != "events" && r.Op != "value") return Outcome.Unsupported("operation " + r.Op);

        var d = InputDecoder.Decode(r.Input);
        if (d.Failure != null) return Outcome.Error(d.Failure, r.Op == "events" ? [] : null).Emulating("decode");
        var o = r.Op == "events" ? Events(d.Text!) : Value(d.Text!);
        return d.Transformed ? o.Emulating("decode") : o;
    }

    // SharpYaml refuses a repeated key with a plain YamlException and no type or code of its own, so the
    // message is the only signal. It is matched in full, mark included, so that no other error takes the
    // category: `(Lin: 0, Col: 10, Chr: 10) - (Lin: 0, Col: 11, Chr: 11): Duplicate mapping key 'a'.`
    static readonly System.Text.RegularExpressions.Regex DuplicateKeyMessage = new(
        @"\A\(Lin: \d+, Col: \d+, Chr: \d+\) - \(Lin: \d+, Col: \d+, Chr: \d+\): Duplicate mapping key '.*'\.\z",
        System.Text.RegularExpressions.RegexOptions.Singleline);

    internal static bool IsDuplicateKey(Exception e) =>
        e.GetType() == typeof(YamlException) && DuplicateKeyMessage.IsMatch(e.Message);

    /// <summary>SharpYaml positions are 0-based; the kit's are 1-based.</summary>
    internal static Failure Describe(Exception e, string text)
    {
        // The category comes from the exception type the library chose; its message is never read.
        var category = e switch
        {
            SyntaxErrorException or SemanticErrorException => "syntax",
            _ when IsDuplicateKey(e) => "duplicate-key",
            _ => "other",
        };
        // An exception with an empty mark has no position to report; a syntax error at the very start does.
        if (e is YamlException y && (category == "syntax" || y.Start.Index > 0 || y.Start.Line > 0 || y.Start.Column > 0))
            return new Failure(category, e.Message, y.Start.Line + 1, Positions.CharColumn(text, y.Start.Index, y.Start.Column + 1));
        return new Failure(category, e.Message);
    }

    static Outcome Events(string text)
    {
        var lines = new List<string>();
        // Only the library's own steps are inside the try: whatever it throws is its answer, while a
        // failure in the adapter's formatting must not be mistaken for one.
        var failure = Host.Library(() =>
        {
            var parser = Parser.CreateParser(new StringReader(text));
            while (true)
            {
                try { if (!parser.MoveNext()) return null; }
                catch (Exception e) { return Host.Own(e); }
                Line(parser.Current!, lines);
            }
        });
        return failure == null ? Outcome.EventsOk(lines) : Outcome.Error(Describe(failure, text), lines);
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
                lines.Add("+MAP" + (ms.Style == YamlStyle.Flow ? " {}" : "") + Props(ms.Anchor, ms.Tag));
                break;
            case MappingEnd: lines.Add("-MAP"); break;
            case SequenceStart ss:
                lines.Add("+SEQ" + (ss.Style == YamlStyle.Flow ? " []" : "") + Props(ss.Anchor, ss.Tag));
                break;
            case SequenceEnd: lines.Add("-SEQ"); break;
            case Scalar s:
                lines.Add("=VAL" + Props(s.Anchor, s.Tag) + " " + Mark(s.Style) + EventNotation.Escape(s.Value));
                break;
            case AnchorAlias a: lines.Add("=ALI *" + a.Value); break;
            default: throw new InvalidOperationException("unknown event " + e.GetType().Name);
        }
    }

    static string Props(string? anchor, string? tag)
    {
        var s = new StringBuilder();
        if (!string.IsNullOrEmpty(anchor)) s.Append(" &").Append(anchor);
        if (!string.IsNullOrEmpty(tag)) s.Append(" <").Append(tag).Append('>');
        return s.ToString();
    }

    internal static char Mark(ScalarStyle style) => style switch
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
                var stream = YamlStream.Load(new StringReader(text));
                foreach (var doc in stream)
                    list.Add(doc.Contents?.ToObject<object>());
                return (list, (Exception?)null);
            }
            catch (Exception e) { return (list, Host.Own(e)); }
        });
        if (read.Item2 != null) return Outcome.Error(Describe(read.Item2, text));

        try
        {
            var json = Host.Convert(() =>
            {
                var budget = new Budget();
                var sb = new StringBuilder("[");
                for (int i = 0; i < read.Item1.Count; i++)
                {
                    if (i > 0) sb.Append(',');
                    sb.Append(Convert(read.Item1[i], budget).ToJson());
                }
                return sb.Append(']').ToString();
            });
            return Outcome.ValueOk(json);
        }
        // A shared alias graph has no finite value to write down; the kit grades the cost it measured.
        catch (TooLargeException) { return Outcome.Accepted(); }
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
