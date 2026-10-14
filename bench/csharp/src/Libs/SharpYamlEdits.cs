using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;
using SharpYaml;
using SharpYaml.Syntax;

namespace OneYamlKit.Bench.Libs;

/// <summary>Edits a <c>YamlSyntaxTree</c> from RFC 6902 steps. The library replaces a range of source
/// characters and nothing more: finding the range of a node, writing the YAML text for a new value in a style
/// that can hold it, and removing a whole entry are all the caller's work, so this is harness code and a cell
/// that uses it is reported as <c>harness-rendered</c>. Everything here runs inside the clock: the lookup walks
/// the tokens of the tree that was parsed before it, and the replacement text follows the style of the value it
/// replaces. Changes are applied last first, so that an earlier change never moves a later one.</summary>
public static class SharpYamlEdits
{
    sealed class Node
    {
        public required string Kind; // scalar, sequence, mapping, alias, empty
        public int Start;            // first character
        public int Last;             // one past the last character
        public string Text = "";     // a scalar's source text, quotes and block header included
        public ScalarStyle Style;
        public bool Flow, Implicit;  // implicit: a block sequence that is not indented under its key
        public List<Node> Items = [];
        public Node? Parent;
        public int Dash = -1;        // a sequence item: where its dash is
        public Node? PendingKey;     // a mapping being read: the key still waiting for its value
        public bool Waiting;         // a mapping after its colon, a sequence after its dash: the next node is the value
    }

    static Node Empty() => new() { Kind = "empty" };

    static ScalarStyle StyleOf(string raw) => raw.Length == 0 ? ScalarStyle.Plain : raw[0] switch
    {
        '\'' => ScalarStyle.SingleQuoted,
        '"' => ScalarStyle.DoubleQuoted,
        '|' => ScalarStyle.Literal,
        '>' => ScalarStyle.Folded,
        _ => ScalarStyle.Plain,
    };

    /// <summary>The value of a key from its source text: quotes removed and the escapes a key can hold resolved.</summary>
    static string KeyText(Node k)
    {
        var t = k.Text;
        switch (k.Style)
        {
            case ScalarStyle.SingleQuoted when t.Length >= 2: return t[1..^1].Replace("''", "'");
            case ScalarStyle.DoubleQuoted when t.Length >= 2:
                {
                    var sb = new StringBuilder();
                    for (int i = 1; i < t.Length - 1; i++)
                    {
                        if (t[i] != '\\' || i + 1 >= t.Length - 1) { sb.Append(t[i]); continue; }
                        char c = t[++i];
                        switch (c)
                        {
                            case 'n': sb.Append('\n'); break;
                            case 't': sb.Append('\t'); break;
                            case 'r': sb.Append('\r'); break;
                            case '0': sb.Append('\0'); break;
                            case 'x' when i + 2 < t.Length: sb.Append((char)int.Parse(t.AsSpan(i + 1, 2), NumberStyles.HexNumber, CultureInfo.InvariantCulture)); i += 2; break;
                            case 'u' when i + 4 < t.Length: sb.Append((char)int.Parse(t.AsSpan(i + 1, 4), NumberStyles.HexNumber, CultureInfo.InvariantCulture)); i += 4; break;
                            default: sb.Append(c); break;
                        }
                    }
                    return sb.ToString();
                }
            default: return t;
        }
    }

    /// <summary>The tokens with the zero-width structure markers that share one position put in a fixed order. The tree
    /// lists them in an order that varies with the surroundings (a key marker before or after the end of the block
    /// it follows, before or after the start of the block it opens), so the walk below would otherwise read the same
    /// structure two ways. Ends come first, then starts, then keys.</summary>
    static List<YamlSyntaxToken> Ordered(IReadOnlyList<YamlSyntaxToken> tokens)
    {
        static int Rank(YamlSyntaxKind k) => k switch
        {
            YamlSyntaxKind.BlockEnd => 0,
            YamlSyntaxKind.BlockMappingStart or YamlSyntaxKind.BlockSequenceStart => 1,
            YamlSyntaxKind.Key => 2,
            _ => -1,
        };
        var result = new List<YamlSyntaxToken>(tokens.Count);
        for (int i = 0; i < tokens.Count; i++)
        {
            var t = tokens[i];
            if (Rank(t.Kind) < 0 || t.Span.Start.Index != t.Span.End.Index) { result.Add(t); continue; }
            int j = i + 1;
            while (j < tokens.Count && Rank(tokens[j].Kind) >= 0 && tokens[j].Span.Start.Index == t.Span.Start.Index && tokens[j].Span.End.Index == t.Span.Start.Index) j++;
            var run = new List<YamlSyntaxToken>(j - i);
            for (int k = i; k < j; k++) run.Add(tokens[k]);
            result.AddRange(run.OrderBy(x => Rank(x.Kind)));
            i = j - 1;
        }
        return result;
    }

    /// <summary>Reads the structure of the first document from the tree's tokens. A block sequence directly under
    /// a mapping key has no start or end token of its own, so it begins at its first dash and ends when the next
    /// key or the end of the mapping arrives; an empty value has no token at all and is noticed by the token that
    /// follows its colon.</summary>
    static Node Index(YamlSyntaxTree tree)
    {
        Node? root = null;
        var stack = new List<Node>();
        int dash = -1;
        Node? Top() => stack.Count > 0 ? stack[^1] : null;

        void Attach(Node n)
        {
            var top = Top();
            if (top == null) { root ??= n; return; }
            n.Parent = top;
            if (top.Kind == "mapping")
            {
                if (top.Waiting)
                {
                    top.Items.Add(top.PendingKey ?? Empty());
                    top.Items.Add(n);
                    top.PendingKey = null;
                    top.Waiting = false;
                }
                else
                {
                    if (top.PendingKey != null) { top.Items.Add(top.PendingKey); top.Items.Add(Empty()); }
                    top.PendingKey = n;
                }
            }
            else
            {
                n.Dash = dash;
                dash = -1;
                top.Items.Add(n);
                top.Waiting = false;
            }
        }

        void Finish(Node n, int? flowEnd)
        {
            if (n.Kind == "mapping" && (n.PendingKey != null || n.Waiting))
            {
                n.Items.Add(n.PendingKey ?? Empty());
                n.Items.Add(Empty());
                n.PendingKey = null;
                n.Waiting = false;
            }
            n.Last = flowEnd ?? (n.Items.Count > 0 ? n.Items.Max(c => c.Last) : n.Start);
        }

        void Pop(int? flowEnd)
        {
            var n = stack[^1];
            stack.RemoveAt(stack.Count - 1);
            Finish(n, flowEnd);
        }

        void CloseImplicit() { while (Top() is { Implicit: true }) Pop(null); }

        var tokens = Ordered(tree.Tokens);
        for (int ti = 0; ti < tokens.Count; ti++)
        {
            var tk = tokens[ti];
            switch (tk.Kind)
            {
                case YamlSyntaxKind.DocumentStart or YamlSyntaxKind.DocumentEnd:
                    if (root != null) goto done;
                    break;
                case YamlSyntaxKind.Key:
                    CloseImplicit();
                    if (Top() is { Kind: "mapping", Waiting: true } m)
                    {
                        m.Items.Add(m.PendingKey ?? Empty());
                        m.Items.Add(Empty());
                        m.PendingKey = null;
                        m.Waiting = false;
                    }
                    break;
                case YamlSyntaxKind.BlockMappingStart or YamlSyntaxKind.FlowMappingStart:
                    {
                        var n = new Node { Kind = "mapping", Start = tk.Span.Start.Index, Flow = tk.Kind == YamlSyntaxKind.FlowMappingStart };
                        Attach(n);
                        stack.Add(n);
                        break;
                    }
                case YamlSyntaxKind.BlockSequenceStart or YamlSyntaxKind.FlowSequenceStart:
                    {
                        var n = new Node { Kind = "sequence", Start = tk.Span.Start.Index, Flow = tk.Kind == YamlSyntaxKind.FlowSequenceStart };
                        Attach(n);
                        stack.Add(n);
                        break;
                    }
                case YamlSyntaxKind.BlockEntry:
                    {
                        var top = Top();
                        if (top is { Kind: "mapping", Waiting: true })
                        {
                            var q = new Node { Kind = "sequence", Start = tk.Span.Start.Index, Implicit = true };
                            Attach(q);
                            stack.Add(q);
                            top = q;
                        }
                        if (top is { Kind: "sequence" })
                        {
                            if (top.Waiting) top.Items.Add(Empty());
                            top.Waiting = true;
                        }
                        dash = tk.Span.Start.Index;
                        break;
                    }
                case YamlSyntaxKind.FlowEntry:
                    if (Top() is { Kind: "mapping", Waiting: true } fm)
                    {
                        fm.Items.Add(fm.PendingKey ?? Empty());
                        fm.Items.Add(Empty());
                        fm.PendingKey = null;
                        fm.Waiting = false;
                    }
                    break;
                case YamlSyntaxKind.Value:
                    if (Top() is { Kind: "mapping" } vm)
                    {
                        vm.PendingKey ??= Empty();
                        vm.Waiting = true;
                    }
                    break;
                case YamlSyntaxKind.Scalar:
                    Attach(new Node { Kind = "scalar", Start = tk.Span.Start.Index, Last = tk.Span.End.Index, Text = tk.Text, Style = StyleOf(tk.Text) });
                    break;
                case YamlSyntaxKind.AnchorAlias:
                    Attach(new Node { Kind = "alias", Start = tk.Span.Start.Index, Last = tk.Span.End.Index, Text = tk.Text });
                    break;
                case YamlSyntaxKind.BlockEnd:
                    CloseImplicit();
                    if (stack.Count > 0) Pop(null);
                    break;
                case YamlSyntaxKind.FlowSequenceEnd or YamlSyntaxKind.FlowMappingEnd:
                    if (stack.Count > 0) Pop(tk.Span.Start.Index + 1);
                    break;
            }
        }
    done:
        while (stack.Count > 0) Pop(null);
        return root ?? throw new InvalidOperationException("the document has no content");
    }

    /// <summary>Applies the steps to the tree and returns the edited tree. Steps the harness cannot express
    /// (through an alias, a collection value, removal inside a flow collection) throw <see cref="Unsupported"/>.</summary>
    public static YamlSyntaxTree Apply(YamlSyntaxTree tree, IReadOnlyList<Op> ops)
    {
        var text = tree.Text;
        var root = Index(tree);
        var edits = new List<(int Start, int Length, string Raw)>();
        foreach (var op in ops)
        {
            var node = root;
            Node? key = null;
            foreach (var token in Pointer.Tokens(op.Path))
            {
                if (node.Kind == "alias") throw Support.No("an edit through an alias is not prepared by the harness");
                if (node.Kind == "mapping")
                {
                    Node? hit = null;
                    for (int i = 0; i + 1 < node.Items.Count && hit == null; i += 2)
                        if (node.Items[i].Kind == "scalar" && KeyText(node.Items[i]) == token) { key = node.Items[i]; hit = node.Items[i + 1]; }
                    node = hit ?? throw Support.No($"key {token} not found");
                }
                else if (node.Kind == "sequence")
                {
                    if (!int.TryParse(token, NumberStyles.None, CultureInfo.InvariantCulture, out var index) || index >= node.Items.Count)
                        throw Support.No("pointer does not follow the document");
                    key = null;
                    node = node.Items[index];
                }
                else throw Support.No("pointer does not follow the document");
            }
            switch (op.Name)
            {
                case "replace":
                    if (node.Kind == "empty") throw Support.No("an empty value has no text to replace");
                    if (node.Kind != "scalar") throw Support.No("replacing a collection is not prepared by the harness");
                    if (op.Value == null || op.Value is J.Arr or J.Obj) throw Support.No("a collection value is not prepared by the harness");
                    edits.Add((node.Start, node.Last - node.Start, Render(text, node, text[node.Start..node.Last], op.Value)));
                    break;
                case "remove":
                    {
                        // A mapping entry is its key and value; a sequence item is its dash and node.
                        bool isEntry = key != null;
                        if ((isEntry ? key!.Parent : node.Parent) is { Flow: true }) throw Support.No("removal inside a flow collection is not prepared by the harness");
                        int from = isEntry ? key!.Start : node.Dash >= 0 ? node.Dash : throw Support.No("the sequence item has no dash before it");
                        int ls = LineStart(text, from);
                        if (text[ls..from].Trim(' ').Length != 0) throw Support.No("the removed entry does not start its line");
                        int end = node.Kind == "empty" ? key!.Last : node.Last;
                        int le = end > 0 && text[end - 1] == '\n' ? end : LineEnd(text, end);
                        edits.Add((ls, le - ls, ""));
                    }
                    break;
                default:
                    throw Support.No($"operation {op.Name} is not an edit of this API");
            }
        }
        edits.Sort((a, b) => b.Start.CompareTo(a.Start));
        foreach (var (start, length, raw) in edits) tree = tree.WithTextChange(start, length, raw);
        return tree;
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
