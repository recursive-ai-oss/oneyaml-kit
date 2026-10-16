#if ONEYAML_LINKED
using System.Numerics;
using System.Text;
using OneYaml;
using OneYamlKit.Adapter;

/// <summary>Kit values from the nodes of a YamlDocument. The document, not the generic <c>object</c> model, keeps
/// what a value case compares: the type of a key, a key that appears twice, and the tag on a node.</summary>
static class Values
{
    // The tags the Core schema resolves; any other tag stays visible as a $tagged wrapper.
    static readonly HashSet<string> Resolved = new(StringComparer.Ordinal)
    {
        "!",
        "tag:yaml.org,2002:str", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float", "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:null", "tag:yaml.org,2002:map", "tag:yaml.org,2002:seq",
    };

    // An alias that nests inside its own anchor would recurse without end; the library's alias budget does
    // not bound the depth of a walk made from outside, so the adapter does.
    const int MaxDepth = 50_000;

    // A bomb of long strings is bounded by size as well as by node count.
    const long MaxChars = 256_000_000;

    [ThreadStatic] static long _chars;

    public static Outcome Run(Request r)
    {
        var (doc, error) = Lib.Parse(r.Input);
        if (error != null) return Outcome.Error(Lib.Describe(error));
        using var _ = doc!;

        var markers = DocumentMarkers.Scan(r.Input);
        int count = doc!.Documents.Count;
        bool aligned = markers != null && markers.Aligned(count);

        try
        {
            var result = Host.Convert<(string? Json, YamlException? Error)>(() =>
            {
                try
                {
                    var budget = new Budget();
                    _chars = 0;
                    var sb = new StringBuilder("[");
                    for (int i = 0; i < count; i++)
                    {
                        if (i > 0) sb.Append(',');
                        var tags = aligned ? markers!.Documents[i].Tags : [];
                        sb.Append(Convert(doc.Documents[i], tags, budget, 0).ToJson());
                    }
                    return (sb.Append(']').ToString(), null);
                }
                catch (YamlException e) { return (null, e); }
                catch (Exception e) when (e is not TooLargeException and not Decline) { throw Host.Own(e); }
            });
            return result.Error != null ? Outcome.Error(Lib.Describe(result.Error)) : Outcome.ValueOk(result.Json!);
        }
        // A shared alias graph has no finite value to write down; the kit grades the cost it measured.
        catch (TooLargeException) { return Outcome.Accepted(); }
        catch (Decline d) { return Outcome.Unsupported(d.Message); }
    }

    static KV Convert(YamlNode n, IReadOnlyDictionary<string, string> tags, Budget budget, int depth)
    {
        budget.Spend();
        if (depth > MaxDepth) throw new TooLargeException("the value nests deeper than the adapter renders");
        if (n.Kind == YamlNodeKind.Alias) return Convert(n.Target, tags, budget, depth + 1);

        KV value;
        switch (n.Kind)
        {
            case YamlNodeKind.Scalar:
                value = Scalar(n);
                break;
            case YamlNodeKind.Mapping:
                {
                    var pairs = new List<KeyValuePair<KV, KV>>();
                    foreach (var e in n.Entries)
                        pairs.Add(new(Convert(e.Key, tags, budget, depth + 1), Convert(e.Value, tags, budget, depth + 1)));
                    value = new KV.Map(pairs);
                    break;
                }
            default:
                {
                    var items = new List<KV>();
                    foreach (var item in n.Items) items.Add(Convert(item, tags, budget, depth + 1));
                    value = new KV.Seq(items);
                    break;
                }
        }

        if (n.RawTag is { Length: > 0 } raw)
        {
            string full = DocumentMarkers.ResolveTag(raw, tags);
            if (!Resolved.Contains(full)) return new KV.Tagged(full, value);
        }
        return value;
    }

    static KV Text(string s)
    {
        _chars += s.Length;
        if (_chars > MaxChars) throw new TooLargeException("the value holds more than " + MaxChars + " characters of text; the adapter does not render it");
        return new KV.Str(s);
    }

    static KV Scalar(YamlNode n)
    {
        object? v = n.GetValue<object>();
        return v switch
        {
            null => KV.Null.Instance,
            bool b => new KV.Bool(b),
            string s => Text(s),
            long l => new KV.Int(l.ToString(System.Globalization.CultureInfo.InvariantCulture)),
            BigInteger bi => KV.Int.Of(bi),
            double d => new KV.Float(d),
            _ => throw new Decline("the adapter cannot describe a " + v.GetType().FullName),
        };
    }
}
#endif
