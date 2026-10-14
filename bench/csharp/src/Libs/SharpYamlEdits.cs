using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;
using SharpYaml;
using SharpYaml.Events;

namespace OneYamlKit.Bench.Libs;

/// <summary>Works out, from the workload's pointers and before the clock, the text changes that
/// <c>YamlSyntaxTree.WithTextChange</c> needs. The library replaces a range of source characters and
/// nothing more: finding the range of a node, writing the YAML text for a new value in a style that can
/// hold it, and removing a whole entry are all the caller's work. The nodes come from the library's own
/// event parser (its start and end marks); the value text follows the style of the value it replaces.
/// Changes are returned last first, so that an earlier change never moves a later one.</summary>
public static class SharpYamlEdits
{
    sealed class Node
    {
        public required string Kind; // scalar, sequence, mapping, alias
        public int Start;            // first character
        public int Last;             // one past the last character
        public string Text = "";
        public string? Anchor, Tag;
        public ScalarStyle Style;
        public bool Flow;
        public List<Node> Items = [];
        public Node? Parent;
    }

    static string? Nz(string? s) => string.IsNullOrEmpty(s) ? null : s;

    static Node Load(string text, int docIndex)
    {
        var stack = new Stack<Node>();
        Node? root = null;
        int doc = -1;
        void Add(Node n)
        {
            if (doc != docIndex) return;
            if (stack.Count > 0) { n.Parent = stack.Peek(); stack.Peek().Items.Add(n); } else root = n;
        }
        var parser = Parser.CreateParser(new StringReader(text));
        while (parser.MoveNext())
        {
            var e = parser.Current;
            switch (e)
            {
                case DocumentStart: doc++; break;
                case Scalar s:
                    Add(new Node { Kind = "scalar", Start = s.Start.Index, Last = s.End.Index, Text = s.Value, Style = s.Style, Anchor = Nz(s.Anchor), Tag = Nz(s.Tag) });
                    break;
                case AnchorAlias a:
                    Add(new Node { Kind = "alias", Start = a.Start.Index, Last = a.End.Index, Text = a.Value });
                    break;
                case SequenceStart ss:
                    if (doc == docIndex) { var n = new Node { Kind = "sequence", Start = ss.Start.Index, Flow = ss.Style == YamlStyle.Flow }; Add(n); stack.Push(n); }
                    break;
                case MappingStart ms:
                    if (doc == docIndex) { var n = new Node { Kind = "mapping", Start = ms.Start.Index, Flow = ms.Style == YamlStyle.Flow }; Add(n); stack.Push(n); }
                    break;
                case SequenceEnd or MappingEnd:
                    if (doc == docIndex)
                    {
                        var n = stack.Pop();
                        // A flow collection ends at its closing bracket; a block collection at the end of its last child.
                        n.Last = n.Flow ? e.Start.Index + 1 : n.Items.Count > 0 ? n.Items.Max(c => c.Last) : n.Start;
                    }
                    break;
            }
        }
        return root ?? throw new InvalidOperationException("the document has no content");
    }

    public static List<(int Start, int Length, string Raw)> Plan(Workload w)
    {
        var text = w.InputText;
        var root = Load(text, 0);
        var edits = new List<(int, int, string)>();
        foreach (var op in w.EditOps)
        {
            var segs = Pointer.Segments(w.BaseValue, op.Path, false);
            var node = root;
            Node? parentMapKey = null;
            foreach (var seg in segs)
            {
                if (node.Kind == "alias") throw Support.No("an edit through an alias is not prepared by the harness");
                if (seg.IsKey)
                {
                    if (node.Kind != "mapping") throw Support.No("pointer does not follow the document");
                    Node? hit = null;
                    for (int i = 0; i + 1 < node.Items.Count && hit == null; i += 2)
                        if (node.Items[i].Kind == "scalar" && node.Items[i].Text == seg.Key) { parentMapKey = node.Items[i]; hit = node.Items[i + 1]; }
                    node = hit ?? throw Support.No($"key {seg.Key} not found");
                }
                else
                {
                    if (node.Kind != "sequence" || seg.Index >= node.Items.Count) throw Support.No("pointer does not follow the document");
                    parentMapKey = null;
                    node = node.Items[seg.Index];
                }
            }
            switch (op.Name)
            {
                case "replace":
                    if (node.Kind != "scalar") throw Support.No("replacing a collection is not prepared by the harness");
                    if (op.Value == null || op.Value is J.Arr or J.Obj) throw Support.No("a collection value is not prepared by the harness");
                    {
                        var original = text[node.Start..node.Last];
                        if (original.StartsWith('&') || original.StartsWith('!')) throw Support.No("the scalar's span includes its anchor or tag");
                        edits.Add((node.Start, node.Last - node.Start, Render(text, node, original, op.Value)));
                    }
                    break;
                case "remove":
                    {
                        // A mapping entry is its key and value; a sequence item is its dash and node.
                        bool isEntry = parentMapKey != null && node.Parent?.Kind == "mapping";
                        int from = isEntry ? parentMapKey!.Start : DashStart(text, node.Start);
                        if (node.Parent is { Flow: true }) throw Support.No("removal inside a flow collection is not prepared by the harness");
                        int ls = LineStart(text, from);
                        if (text[ls..from].Trim(' ').Length != 0) throw Support.No("the removed entry does not start its line");
                        int end = node.Last;
                        int le = end > 0 && text[end - 1] == '\n' ? end : LineEnd(text, end);
                        edits.Add((ls, le - ls, ""));
                    }
                    break;
                default:
                    throw Support.No($"operation {op.Name} is not an edit of this API");
            }
        }
        return edits.OrderByDescending(e => e.Item1).ToList();
    }

    static int LineStart(string t, int i)
    {
        while (i > 0 && t[i - 1] != '\n') i--;
        return i;
    }

    static int LineEnd(string t, int i)
    {
        int n = t.IndexOf('\n', i);
        return n < 0 ? t.Length : n + 1;
    }

    /// <summary>The position of the dash in front of a sequence item that starts at <paramref name="start"/>.</summary>
    static int DashStart(string t, int start)
    {
        int i = start - 1;
        while (i >= 0 && (t[i] == ' ' || t[i] == '\t')) i--;
        if (i < 0 || t[i] != '-') throw Support.No("the sequence item has no dash before it");
        return i;
    }

    // ---- the text of a new value ---------------------------------------------------------------------

    static string Render(string text, Node node, string original, J value)
    {
        switch (value)
        {
            case J.Null: return "null";
            case J.Bool b: return b.V ? "true" : "false";
            case J.Int i: return i.V.ToString(CultureInfo.InvariantCulture);
            case J.Float f:
                {
                    var s = f.V.ToString("R", CultureInfo.InvariantCulture);
                    return s.IndexOfAny(['.', 'E', 'e']) < 0 ? s + ".0" : s;
                }
        }
        var str = ((J.Str)value).V;
        switch (node.Style)
        {
            case ScalarStyle.Literal or ScalarStyle.Folded:
                return Block(original, node.Style, str, text) ?? DoubleQuoted(str);
            case ScalarStyle.DoubleQuoted:
                return DoubleQuoted(str);
            case ScalarStyle.SingleQuoted:
                return Printable(str, true) ? "'" + str.Replace("'", "''") + "'" : DoubleQuoted(str);
            default:
                if (PlainSafe(str)) return str;
                return Printable(str, false) ? "'" + str.Replace("'", "''") + "'" : DoubleQuoted(str);
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
        if (s.Length == 0 || !Printable(s, false)) return false;
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
        if (body.Length == 0 || !Printable(body.Replace("\n", ""), true)) return null;
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
        var indent = IndentOf(original, eol);
        var sb = new StringBuilder();
        sb.Append(mark).Append(chomp).Append(tail);
        foreach (var l in lines) sb.Append(nl).Append(l.Length == 0 ? "" : indent + l);
        for (int k = 1; k < trailing; k++) sb.Append(nl);
        // The original span ends with its last line break unless the document ends there.
        if (original.EndsWith('\n') || original.EndsWith('\r')) sb.Append(nl);
        return sb.ToString();
    }

    static string IndentOf(string original, int eol)
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
