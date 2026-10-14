using System.Numerics;
using System.Text;
using OneYamlKit.Adapter;
using VYaml.Parser;
using VYaml.Serialization;

return Host.Serve(new VYamlKitAdapter());

/// <summary>VYaml through its UTF-8 event parser (events) and YamlSerializer into plain objects (values).
/// The library has no edit or patch path, so the adapter declares none.</summary>
sealed class VYamlKitAdapter : IAdapter
{
    public string Name => "vyaml";

    public string Version { get; } = PackageVersions.Of("VYaml");

    public string[] Capabilities => ["events", "value", "error-category", "error-events"];

    // Only VYaml's own exception hierarchy is a rejection. Anything else escaping the library (it lets a
    // duplicate mapping key surface as the runtime's ArgumentException, for instance) is a defect, and the
    // process ends so the kit records a crash.
    public bool IsLibraryException(Exception e) => e is VYaml.YamlException;

    // decode: VYaml reads UTF-8 bytes (a byte order mark is passed through as it is), so UTF-16 and
    //   UTF-32 input is converted to UTF-8 by the adapter first.
    // style: the event API has no scalar style, flow/block marker or explicit document marker; the
    //   adapter reads them from the public tokenizer and puts them into the event lines (TokenFacts.cs).
    // tag: the event API gives a tag as handle and suffix; the adapter writes out the full tag for the
    //   `!`, `!!` and verbatim forms.
    public string[] Emulates => ["decode", "style", "tag"];

    public IReadOnlyDictionary<string, string> Info { get; } = new Dictionary<string, string>
    {
        ["events"] = "VYaml.Parser.YamlParser.FromBytes, with style facts from Utf8YamlTokenizer",
        ["value"] = "YamlSerializer.DeserializeMultipleDocuments<object> over the bytes, default options",
        ["library stack"] = $"{Host.LibraryStackBytes / (1024 * 1024)} MiB thread",
        ["runtime"] = System.Runtime.InteropServices.RuntimeInformation.FrameworkDescription,
    };

    /// <summary>The adapter cannot tell a fact the library does not give.</summary>
    sealed class Unknown(string why) : Exception(why);

    public Outcome Run(Request r)
    {
        if (r.Profile != "core") return Outcome.Unsupported("profile " + r.Profile);
        if (r.Op != "events" && r.Op != "value") return Outcome.Unsupported("operation " + r.Op);

        // Bytes go to the library as they are when they are UTF-8, so its own handling of a byte order
        // mark or a malformed sequence is what is measured. Other encodings are converted.
        var d = InputDecoder.Decode(r.Input);
        byte[] bytes = r.Input;
        bool converted = false;
        if (d.Encoding is not UTF8Encoding)
        {
            if (d.Failure != null) return Outcome.Error(d.Failure, r.Op == "events" ? [] : null).Emulating("decode");
            bytes = Encoding.UTF8.GetBytes(d.Text!);
            converted = true;
        }
        Outcome o;
        try { o = r.Op == "events" ? Events(bytes) : Value(bytes); }
        catch (Unknown u) { return Outcome.Unsupported(u.Message); }
        return converted ? o.Emulating("decode") : o;
    }

    static Failure Describe(Exception e)
    {
        // The category comes from the exception type the library chose; its message is never read.
        var category = e is YamlParserException or YamlTokenizerException ? "syntax" : "other";
        return new Failure(category, e.Message);
    }

    static Outcome Events(byte[] utf8)
    {
        var lines = new List<string>();
        var emulated = new SortedSet<string>();
        var facts = Host.Library(() => TokenFacts.Read(utf8));
        var lost = new List<string>();
        var failure = Host.Library(() => Walk(utf8, facts, lines, emulated, lost));
        // When the parser accepted the input but the adapter could not say what some event looks like,
        // there is no honest event stream to give. When the parser rejected it, the rejection stands and
        // the lines before the first unknown event are all the adapter can report.
        if (failure == null && lost.Count > 0) throw new Unknown(lost[0]);
        var o = failure == null ? Outcome.EventsOk(lines) : Outcome.Error(Describe(failure), lines);
        return o.Emulating([.. emulated]);
    }

    static Exception? Walk(byte[] utf8, TokenFacts facts, List<string> lines, SortedSet<string> emulated, List<string> lost)
    {
        int scalar = 0, collection = 0, docStart = 0, docEnd = 0;
        YamlParser parser;
        try { parser = YamlParser.FromBytes(utf8); }
        catch (Exception e) { return Host.Own(e); }
        while (true)
        {
            try { if (!parser.Read()) return null; }
            catch (Exception e) { return Host.Own(e); }
            if (lost.Count > 0) continue;
            try
            {
                switch (parser.CurrentEventType)
                {
                    case ParseEventType.StreamStart: lines.Add("+STR"); break;
                    case ParseEventType.StreamEnd: lines.Add("-STR"); break;
                    case ParseEventType.DocumentStart:
                        emulated.Add("style");
                        lines.Add(Fact(facts.Documents, docStart++).ExplicitStart ? "+DOC ---" : "+DOC");
                        break;
                    case ParseEventType.DocumentEnd:
                        emulated.Add("style");
                        lines.Add(Fact(facts.Documents, docEnd++).ExplicitEnd ? "-DOC ..." : "-DOC");
                        break;
                    case ParseEventType.MappingStart:
                    case ParseEventType.SequenceStart:
                        {
                            bool map = parser.CurrentEventType == ParseEventType.MappingStart;
                            var c = Fact(facts.Collections, collection++);
                            if (c.IsMap != map) throw new Unknown("the tokenizer's collections do not line up with the parser's events");
                            emulated.Add("style");
                            lines.Add((map ? "+MAP" : "+SEQ") + (c.Flow ? (map ? " {}" : " []") : "") + Props(parser, facts, emulated));
                            break;
                        }
                    case ParseEventType.MappingEnd: lines.Add("-MAP"); break;
                    case ParseEventType.SequenceEnd: lines.Add("-SEQ"); break;
                    case ParseEventType.Alias:
                        parser.TryGetCurrentAnchor(out var alias);
                        lines.Add("=ALI *" + alias.Name);
                        break;
                    case ParseEventType.Scalar:
                        {
                            var value = parser.TryGetScalarAsSpan(out var span) ? Encoding.UTF8.GetString(span) : parser.GetScalarAsString() ?? "";
                            char style = ':';
                            // An empty scalar with no token is an implicit empty value, which is plain. A quoted
                            // or block token that follows could belong to it or to the next node; that is not
                            // something the adapter can tell.
                            bool backed = value.Length > 0;
                            if (!backed && scalar < facts.Scalars.Count && facts.Scalars[scalar] != ':')
                                throw new Unknown("an empty scalar next to a quoted or block scalar: the adapter cannot tell which has the token");
                            if (backed) style = Fact(facts.Scalars, scalar++);
                            emulated.Add("style");
                            lines.Add("=VAL" + Props(parser, facts, emulated) + " " + style + EventNotation.Escape(value));
                            break;
                        }
                    default: throw new InvalidOperationException("unknown event " + parser.CurrentEventType);
                }
            }
            catch (Unknown u) { lost.Add(u.Message); }
        }
    }

    static T Fact<T>(List<T> list, int index) =>
        index < list.Count ? list[index] : throw new Unknown("the tokenizer saw fewer nodes than the parser reported");

    static string Props(YamlParser parser, TokenFacts facts, SortedSet<string> emulated)
    {
        var s = new StringBuilder();
        if (parser.TryGetCurrentAnchor(out var anchor)) s.Append(" &").Append(anchor.Name);
        if (parser.TryGetCurrentTag(out var tag))
        {
            // %TAG directives change what a handle means and VYaml does not say which were declared.
            if (facts.HasTagDirective) throw new Unknown("the stream declares %TAG handles, which VYaml does not expose");
            emulated.Add("tag");
            string full = tag.Handle switch
            {
                "!!" => "tag:yaml.org,2002:" + tag.Suffix,
                "!" => "!" + tag.Suffix,
                "" => tag.Suffix,
                _ => throw new Unknown($"tag handle {tag.Handle} comes from a %TAG directive, which VYaml does not expose"),
            };
            s.Append(" <").Append(full).Append('>');
        }
        return s.ToString();
    }

    static Outcome Value(byte[] utf8)
    {
        var read = Host.Library(() =>
        {
            var list = new List<object?>();
            try
            {
                foreach (var doc in YamlSerializer.DeserializeMultipleDocuments<object>(new ReadOnlyMemory<byte>(utf8)))
                    list.Add(doc);
                return (list, (Exception?)null);
            }
            catch (Exception e) { return (list, Host.Own(e)); }
        });
        if (read.Item2 != null) return Outcome.Error(Describe(read.Item2));

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
