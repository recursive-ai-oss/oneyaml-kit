#if ONEYAML_LINKED
using System.Text;
using OneYaml;
using OneYamlKit.Adapter;

/// <summary>The kit's event lines, derived from the nodes of a YamlDocument.</summary>
static class Events
{
    sealed class Out
    {
        public readonly List<string> Lines = ["+STR"];
        public bool Tagged;
    }

    public static Outcome Run(Request r)
    {
        var (doc, error) = Lib.Parse(r.Input, allowDuplicateKeys: true);
        if (error != null) return Outcome.Error(Lib.Describe(error));
        using var _ = doc!;

        var markers = DocumentMarkers.Scan(r.Input);
        int count = doc!.Documents.Count;
        if (markers == null || !markers.Aligned(count))
            return Outcome.Unsupported($"document markers: the line scan found {markers?.Documents.Count.ToString() ?? "no"} documents and the library {count}");

        var o = new Out();
        try
        {
            Host.Library<object?>(() =>
            {
                for (int i = 0; i < count; i++)
                {
                    var info = markers.Documents[i];
                    o.Lines.Add(info.ExplicitStart ? "+DOC ---" : "+DOC");
                    Node(doc.Documents[i], info.Tags, o);
                    o.Lines.Add(info.ExplicitEnd ? "-DOC ..." : "-DOC");
                }
                return null;
            });
        }
        catch (YamlException e) { return Outcome.Error(Lib.Describe(e)); }
        catch (Decline d) { return Outcome.Unsupported(d.Message); }
        o.Lines.Add("-STR");

        var ok = Outcome.EventsOk(o.Lines).Emulating("style");
        return o.Tagged ? ok.Emulating("tag") : ok;
    }

    static void Node(YamlNode n, IReadOnlyDictionary<string, string> tags, Out o)
    {
        switch (n.Kind)
        {
            case YamlNodeKind.Alias:
                o.Lines.Add("=ALI *" + n.ToString().TrimStart('*'));
                return;
            case YamlNodeKind.Scalar:
                {
                    string props = Props(n, tags, o);
                    o.Lines.Add("=VAL" + props + " " + Mark(n.Style) + EventNotation.Escape(ScalarText(n)));
                    return;
                }
            case YamlNodeKind.Mapping:
                {
                    string props = Props(n, tags, o);
                    o.Lines.Add("+MAP" + (n.Style == YamlNodeStyle.Flow ? " {}" : "") + props);
                    foreach (var entry in n.Entries)
                    {
                        Node(entry.Key, tags, o);
                        Node(entry.Value, tags, o);
                    }
                    o.Lines.Add("-MAP");
                    return;
                }
            default:
                {
                    string props = Props(n, tags, o);
                    o.Lines.Add("+SEQ" + (n.Style == YamlNodeStyle.Flow ? " []" : "") + props);
                    foreach (var item in n.Items) Node(item, tags, o);
                    o.Lines.Add("-SEQ");
                    return;
                }
        }
    }

    static string Props(YamlNode n, IReadOnlyDictionary<string, string> tags, Out o)
    {
        var sb = new StringBuilder();
        if (n.Anchor is { Length: > 0 } a) sb.Append(" &").Append(a);
        if (n.Tag is { Length: > 0 } t)
        {
            sb.Append(" <").Append(DocumentMarkers.ResolveTag(t, tags)).Append('>');
            o.Tagged = true;
        }
        return sb.ToString();
    }

    static char Mark(YamlNodeStyle style) => style switch
    {
        YamlNodeStyle.SingleQuoted => '\'',
        YamlNodeStyle.DoubleQuoted => '"',
        YamlNodeStyle.Literal => '|',
        YamlNodeStyle.Folded => '>',
        _ => ':',
    };

    /// <summary>The scalar's text after folding and unescaping. <c>GetValue&lt;string&gt;</c> gives it, except
    /// that a plain scalar which reads as null (empty, <c>~</c>, <c>null</c>) comes back as no string at all;
    /// those are one line, so their source text with the properties taken off is the text.</summary>
    internal static string ScalarText(YamlNode n)
    {
        string? s = null;
        try { s = n.GetValue<string>(); }
        catch (YamlException) when (n.Style == YamlNodeStyle.Plain) { /* a tag that does not fit the text: read the source */ }
        catch (YamlException) { throw new Decline("the library does not give the text of a quoted or block scalar whose tag does not fit it"); }
        if (s != null) return s;

        var rest = n.ToString().AsSpan().Trim();
        while (rest.Length > 0 && (rest[0] == '&' || rest[0] == '!'))
        {
            int sp = rest.IndexOfAny(" \t\n\r");
            if (sp < 0) { rest = default; break; }
            rest = rest[sp..].TrimStart();
        }
        return rest.ToString();
    }
}
#endif
