#if ONEYAML_LINKED
using System.Text;
using OneYamlKit.Adapter;

/// <summary>What the document does not carry: whether each document had an explicit start (<c>---</c>) or
/// end (<c>...</c>) marker, and the <c>%TAG</c> directives that apply to it. Found with a scan of the
/// decoded lines: a marker is a line that begins with three dashes or dots followed by a space or the end
/// of the line, which no content line may do (YAML 1.2.2 section 9.1.2).</summary>
sealed class DocumentMarkers
{
    public sealed class Info
    {
        public bool ExplicitStart;
        public bool ExplicitEnd;
        public Dictionary<string, string> Tags = [];
    }

    public List<Info> Documents { get; } = [];

    /// <summary>The library reads a stream with no document (empty, or only comments) as one empty document;
    /// the scan then has none. Both views are given the library's, so the difference shows in the result.</summary>
    public bool Aligned(int libraryCount)
    {
        if (Documents.Count == 0 && libraryCount == 1) Documents.Add(new Info());
        return Documents.Count == libraryCount;
    }

    public static DocumentMarkers? Scan(byte[] input)
    {
        var decoded = InputDecoder.Decode(input);
        if (decoded.Failure != null || decoded.Text == null) return null;
        string text = decoded.Text;
        var result = new DocumentMarkers();
        Info? open = null;
        var pending = new Dictionary<string, string>();

        int pos = 0;
        while (pos < text.Length)
        {
            int end = pos;
            while (end < text.Length && text[end] != '\n' && text[end] != '\r') end++;
            var line = text.AsSpan(pos, end - pos);
            // A byte order mark may begin a document (YAML 1.2.2 section 9.1.1); it is not content.
            if (line.Length > 0 && line[0] == '\uFEFF') line = line[1..];
            pos = end;
            if (pos < text.Length) pos += text[pos] == '\r' && pos + 1 < text.Length && text[pos + 1] == '\n' ? 2 : 1;

            if (IsMarker(line, '-'))
            {
                if (open != null) open.ExplicitEnd = false;
                open = new Info { ExplicitStart = true, Tags = pending };
                result.Documents.Add(open);
                pending = [];
            }
            else if (IsMarker(line, '.'))
            {
                if (open != null) open.ExplicitEnd = true;
                open = null;
                pending = [];
            }
            else if (open == null)
            {
                if (line.Length > 0 && line[0] == '%')
                {
                    Directive(line, pending);
                }
                else if (!IsBlankOrComment(line))
                {
                    open = new Info { ExplicitStart = false, Tags = pending };
                    result.Documents.Add(open);
                    pending = [];
                }
            }
        }
        return result;
    }

    static bool IsMarker(ReadOnlySpan<char> line, char c) =>
        line.Length >= 3 && line[0] == c && line[1] == c && line[2] == c
        && (line.Length == 3 || line[3] == ' ' || line[3] == '\t');

    static bool IsBlankOrComment(ReadOnlySpan<char> line)
    {
        var t = line.TrimStart(" \t");
        return t.IsEmpty || t[0] == '#';
    }

    // %TAG !handle! prefix
    static void Directive(ReadOnlySpan<char> line, Dictionary<string, string> tags)
    {
        var parts = line.ToString().Split([' ', '\t'], StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length >= 3 && parts[0] == "%TAG") tags[parts[1]] = parts[2];
    }

    /// <summary>The full tag of a tag as written in the source, with the handles of the document.</summary>
    public static string ResolveTag(string raw, IReadOnlyDictionary<string, string>? tags)
    {
        if (raw == "!") return "!";
        if (raw.Length >= 3 && raw.StartsWith("!<", StringComparison.Ordinal) && raw[^1] == '>') return raw[2..^1];
        int second = raw.IndexOf('!', 1);
        string handle = second > 0 ? raw[..(second + 1)] : "!";
        string suffix = second > 0 ? raw[(second + 1)..] : raw[1..];
        string? prefix = null;
        if (tags != null && tags.TryGetValue(handle, out var declared)) prefix = declared;
        prefix ??= handle == "!!" ? "tag:yaml.org,2002:" : handle == "!" ? "!" : handle;
        return prefix + Unescape(suffix);
    }

    static string Unescape(string s)
    {
        if (s.IndexOf('%') < 0) return s;
        var bytes = new List<byte>();
        for (int i = 0; i < s.Length; i++)
        {
            if (s[i] == '%' && i + 2 < s.Length && Uri.IsHexDigit(s[i + 1]) && Uri.IsHexDigit(s[i + 2])
                && Convert.ToByte(s.Substring(i + 1, 2), 16) is byte b && b < 0x80)
            {
                // Only an escaped ASCII character is expanded (YAML 1.2.2 section 6.9.1); a UTF-8 sequence stays as written.
                bytes.Add(b);
                i += 2;
            }
            else
            {
                int len = char.IsHighSurrogate(s[i]) && i + 1 < s.Length ? 2 : 1;
                bytes.AddRange(Encoding.UTF8.GetBytes(s.Substring(i, len)));
                i += len - 1;
            }
        }
        return Encoding.UTF8.GetString(bytes.ToArray());
    }
}
#endif
