using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using OneYamlKit.Adapter;
using SharpYaml;
using SharpYaml.Events;
using SharpYaml.Syntax;

/// <summary>
/// The edit path SharpYaml really has: <c>YamlSyntaxTree.WithTextChange(start, length, rawYaml)</c>
/// replaces a span of the source and keeps every other byte. It has no notion of a path or a value,
/// so this class does what a caller has to do: resolve the pointer to the span of a scalar (from the
/// library's own parse events, following an alias to the node it names and taking the first of two equal
/// keys), write the YAML text for the new value in a style that can hold it, and undo everything when a
/// step fails. It supplies no refusal of its own: a write through an alias or an ambiguous key goes ahead,
/// as it would for anyone using the library this way. All of that is declared as emulated behaviour.
/// </summary>
static class Edit
{
    // A node of the parsed document: just enough to resolve a JSON Pointer.
    sealed class Node
    {
        public required string Kind; // scalar, sequence, mapping, alias
        public int Start, End;
        public string Text = "";     // scalar value or alias name
        public string? Anchor, Tag;
        public ScalarStyle Style;
        public List<Node> Items = [];
    }

    sealed class Refusal(string category, string message) : Exception(message)
    {
        public string Category { get; } = category;
    }

    sealed class Decline(string reason) : Exception(reason);

    public static Outcome Patch(Request r)
    {
        var d = InputDecoder.Decode(r.Input);
        if (d.Failure != null)
            return Outcome.Error(d.Failure, output: r.Input).Emulating("decode", "unchanged");
        if (r.Args.TryGetProperty("format", out var f) && f.GetString() != "json-patch")
            return Outcome.Unsupported("merge patch");
        int docIndex = r.Args.TryGetProperty("document", out var di) ? di.GetInt32() : 0;
        if (!r.Args.TryGetProperty("patch", out var ops) || ops.ValueKind != JsonValueKind.Array)
            return Fail("patch-invalid", "a patch is an array of operations", r.Input);

        try
        {
            var tree = Host.Library(() => YamlSyntaxTree.Parse(d.Text!, new YamlSyntaxOptions { IncludeTrivia = true }));
            foreach (var op in ops.EnumerateArray())
            {
                var name = op.ValueKind == JsonValueKind.Object && op.TryGetProperty("op", out var n) ? n.GetString() : null;
                if (name != "replace")
                    throw new Refusal("patch-invalid", $"operation {name ?? "(none)"} is not supported by this adapter");
                if (!op.TryGetProperty("path", out var p) || p.ValueKind != JsonValueKind.String || !op.TryGetProperty("value", out var value))
                    throw new Refusal("patch-invalid", "replace needs a path and a value");
                var text = tree.Text;
                var (start, length, raw) = Plan(text, docIndex, p.GetString()!, value);
                tree = Host.Library(() => tree.WithTextChange(start, length, raw));
            }
            var o = Outcome.OutputOk(d.Encode(tree.ToFullString())).Emulating("pointer", "source-text");
            return d.Transformed ? o.Emulating("decode") : o;
        }
        catch (Refusal e)
        {
            // A path that does not resolve is an answer that depended on the adapter's pointer resolution.
            var o = Fail(e.Category, e.Message, r.Input, ownError: true);
            return e.Category == "patch-path" ? o.Emulating("pointer") : o;
        }
        catch (Decline e) { return Outcome.Unsupported(e.Message); }
        catch (YamlException e)
        {
            // The library's own answer: it rejected the text (the input, or a replacement that made it invalid).
            return Outcome.Error(SharpYamlKitAdapter.Describe(e, d.Text!), output: r.Input).Emulating("unchanged");
        }
    }

    static Outcome Fail(string category, string message, byte[] original, bool ownError = true)
    {
        var o = Outcome.Error(new Failure(category, message), output: original).Emulating("unchanged");
        return ownError ? o.Emulating("error") : o;
    }

    // ---- locating the span ---------------------------------------------------------------------

    static (int start, int length, string raw) Plan(string text, int docIndex, string pointer, JsonElement value)
    {
        if (pointer.Length > 0 && pointer[0] != '/')
            throw new Refusal("patch-invalid", "a JSON Pointer is empty or starts with '/'");
        var tokens = pointer.Length == 0
            ? []
            : pointer[1..].Split('/').Select(t => t.Replace("~1", "/").Replace("~0", "~")).ToArray();

        var (root, anchors) = Host.Library(() => Load(text, docIndex));
        if (root == null) throw new Refusal("patch-path", "the document does not exist");

        var node = root;
        foreach (var t in tokens)
        {
            if (node.Kind == "alias")
            {
                if (!anchors.TryGetValue(node.Text, out var target)) throw new Refusal("patch-path", "unknown alias");
                // An alias is followed to the node it names, as a reader of the library's events would: a
                // write then lands in the anchored text, and nothing refuses it.
                node = target;
            }
            switch (node.Kind)
            {
                case "sequence":
                    if (!Regex.IsMatch(t, "^(0|[1-9][0-9]*)$") || !int.TryParse(t, NumberStyles.None, CultureInfo.InvariantCulture, out var idx) || idx >= node.Items.Count)
                        throw new Refusal("patch-path", $"no item {t}");
                    node = node.Items[idx];
                    break;
                case "mapping":
                    // The event layer has no key lookup, so the first key with that text, in document order.
                    Node? hit = null;
                    for (int i = 0; i + 1 < node.Items.Count && hit == null; i += 2)
                        if (node.Items[i].Kind == "scalar" && node.Items[i].Text == t) hit = node.Items[i + 1];
                    node = hit ?? throw new Refusal("patch-path", $"no key {t}");
                    break;
                default:
                    throw new Refusal("patch-path", "the path goes through a scalar");
            }
        }
        if (node.Kind is "sequence" or "mapping") throw new Decline("replacing a collection needs the adapter to write block or flow YAML");
        if (value.ValueKind is JsonValueKind.Object or JsonValueKind.Array) throw new Decline("a collection value needs the adapter to write block or flow YAML");

        var span = text[node.Start..node.End];
        if (node.Kind == "scalar" && (node.Anchor != null || node.Tag != null) && (span.StartsWith('&') || span.StartsWith('!')))
            throw new Decline("the scalar's span includes its anchor or tag");
        return (node.Start, node.End - node.Start, Render(text, node, value));
    }

    static (Node? root, Dictionary<string, Node> anchors) Load(string text, int docIndex)
    {
        var anchors = new Dictionary<string, Node>();
        var stack = new Stack<Node>();
        Node? root = null;
        int doc = -1;
        var parser = Parser.CreateParser(new StringReader(text));
        void Add(Node n)
        {
            if (doc != docIndex) return;
            if (stack.Count > 0) stack.Peek().Items.Add(n); else root = n;
        }
        while (parser.MoveNext())
        {
            var e = parser.Current;
            switch (e)
            {
                case DocumentStart: doc++; break;
                case Scalar s:
                    {
                        var n = new Node { Kind = "scalar", Start = s.Start.Index, End = s.End.Index, Text = s.Value, Style = s.Style, Anchor = Nz(s.Anchor), Tag = Nz(s.Tag) };
                        if (doc == docIndex && n.Anchor != null) anchors[n.Anchor] = n;
                        Add(n);
                        break;
                    }
                case AnchorAlias a:
                    Add(new Node { Kind = "alias", Start = a.Start.Index, End = a.End.Index, Text = a.Value });
                    break;
                case SequenceStart ss: Open(new Node { Kind = "sequence", Start = ss.Start.Index, Anchor = Nz(ss.Anchor) }); break;
                case MappingStart ms: Open(new Node { Kind = "mapping", Start = ms.Start.Index, Anchor = Nz(ms.Anchor) }); break;
                case SequenceEnd or MappingEnd:
                    if (doc == docIndex) stack.Pop().End = e.End.Index;
                    break;
            }
        }
        return (root, anchors);

        void Open(Node n)
        {
            if (doc != docIndex) return;
            if (n.Anchor != null) anchors[n.Anchor] = n;
            Add(n);
            stack.Push(n);
        }
    }

    static string? Nz(string? s) => string.IsNullOrEmpty(s) ? null : s;

    // ---- writing the replacement text ----------------------------------------------------------

    static string Render(string text, Node node, JsonElement value)
    {
        switch (value.ValueKind)
        {
            case JsonValueKind.Null: return "null";
            case JsonValueKind.True: return "true";
            case JsonValueKind.False: return "false";
            case JsonValueKind.Number: return value.GetRawText();
        }
        var s = value.GetString()!;
        var original = text[node.Start..node.End];
        switch (node.Style)
        {
            case ScalarStyle.Literal or ScalarStyle.Folded when Block(original, node.Style, s, text) is string b:
                return b;
            case ScalarStyle.Literal or ScalarStyle.Folded:
                return DoubleQuoted(s);
            case ScalarStyle.DoubleQuoted:
                return DoubleQuoted(s);
            case ScalarStyle.SingleQuoted when Printable(s, allowTab: true):
                return "'" + s.Replace("'", "''") + "'";
            case ScalarStyle.SingleQuoted:
                return DoubleQuoted(s);
            default:
                if (PlainSafe(s)) return s;
                return Printable(s, allowTab: false) ? "'" + s.Replace("'", "''") + "'" : DoubleQuoted(s);
        }
    }

    static bool Printable(string s, bool allowTab)
    {
        foreach (var c in s)
        {
            if (c == '\t') { if (!allowTab) return false; continue; }
            if (c < 0x20 || c == 0x7f || c == '\u0085' || c == '\u2028' || c == '\u2029' || c == '\uFEFF') return false;
        }
        return true;
    }

    static readonly Regex NotAString = new(
        @"^(~|null|Null|NULL|true|True|TRUE|false|False|FALSE|[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+|[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?|[-+]?\.(inf|Inf|INF)|\.(nan|NaN|NAN))$",
        RegexOptions.CultureInvariant);

    static bool PlainSafe(string s)
    {
        if (s.Length == 0 || !Printable(s, allowTab: false)) return false;
        if (s[0] == ' ' || s[^1] == ' ') return false;
        if ("-?:,[]{}#&*!|>'\"%@`".Contains(s[0])) return false;
        if (s.Contains(": ") || s.Contains(" #") || s.EndsWith(':') || s.IndexOfAny([',', '[', ']', '{', '}']) >= 0) return false;
        return !NotAString.IsMatch(s);
    }

    static string DoubleQuoted(string s)
    {
        var sb = new StringBuilder("\"");
        foreach (var c in s)
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 0x20 || c == 0x7f) sb.Append("\\x").Append(((int)c).ToString("X2", CultureInfo.InvariantCulture));
                    else if (c is '\u0085' or '\u2028' or '\u2029' or '\uFEFF') sb.Append("\\u").Append(((int)c).ToString("X4", CultureInfo.InvariantCulture));
                    else sb.Append(c);
                    break;
            }
        }
        return sb.Append('"').ToString();
    }

    /// <summary>A block scalar holding <paramref name="s"/> exactly, in the original's style and indentation,
    /// or null when no block scalar can hold it.</summary>
    static string? Block(string original, ScalarStyle style, string s, string text)
    {
        var body = s.TrimEnd('\n');
        if (body.Length == 0 || !Printable(body.Replace("\n", ""), allowTab: true)) return null;
        if (body[0] == ' ' || body[0] == '\n') return null; // would need an indentation indicator
        int trailing = s.Length - body.Length;
        var lines = body.Split('\n');
        // A folded scalar joins lines, so only a single line can be written without changing the value.
        char mark = style == ScalarStyle.Folded && lines.Length == 1 ? '>' : '|';
        string chomp = trailing == 0 ? "-" : trailing == 1 ? "" : "+";

        // Keep a trailing comment on the header line.
        int eol = original.IndexOfAny(['\r', '\n']);
        var header = eol < 0 ? original : original[..eol];
        int cut = header.IndexOfAny([' ', '\t']);
        var tail = cut < 0 ? "" : header[cut..];

        string nl = text.Contains("\r\n") ? "\r\n" : text.Contains('\r') && !text.Contains('\n') ? "\r" : "\n";
        var indent = IndentOf(original, eol, text);
        var sb = new StringBuilder();
        sb.Append(mark).Append(chomp).Append(tail);
        foreach (var l in lines) sb.Append(nl).Append(l.Length == 0 ? "" : indent + l);
        for (int k = 1; k < trailing; k++) sb.Append(nl);
        // The original span ends with its last line break unless the document ends there.
        if (original.EndsWith('\n') || original.EndsWith('\r')) sb.Append(nl);
        return sb.ToString();
    }

    static string IndentOf(string original, int eol, string text)
    {
        if (eol >= 0)
        {
            foreach (var line in original[(eol + 1)..].Split('\n'))
            {
                var t = line.TrimEnd('\r');
                if (t.Trim().Length == 0) continue;
                return new string(' ', t.Length - t.TrimStart(' ').Length);
            }
        }
        return "  ";
    }
}
