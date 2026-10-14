using System.Text;

namespace OneYamlKit.Adapter;

/// <summary>The result of turning input bytes into text.</summary>
public sealed class Decoded
{
    public string? Text { get; init; }
    public Failure? Failure { get; init; }
    /// <summary>True when the adapter did more than convert valid UTF-8 to a string: it removed a
    /// byte order mark, converted UTF-16 or UTF-32, or rejected malformed bytes. That work is the
    /// adapter's, not the library's, so responses that depend on it say so.</summary>
    public bool Transformed { get; init; }
    public Encoding? Encoding { get; init; }
    /// <summary>The byte order mark that was removed (empty when there was none).</summary>
    public byte[] Bom { get; init; } = [];

    /// <summary>Text back to bytes in the encoding the input had, with its byte order mark restored.</summary>
    public byte[] Encode(string text)
    {
        var body = Encoding!.GetBytes(text);
        var all = new byte[Bom.Length + body.Length];
        Bom.CopyTo(all, 0);
        body.CopyTo(all, Bom.Length);
        return all;
    }
}

/// <summary>Detects the encoding of a YAML stream (YAML 1.2.2 section 5.2) and decodes it strictly.
/// Most .NET YAML libraries take a string or a text reader, so this step is the adapter's.</summary>
public static class InputDecoder
{
    public static Decoded Decode(byte[] b)
    {
        Encoding enc;
        int skip = 0;
        bool plainUtf8 = false;
        if (b.Length >= 4 && b[0] == 0 && b[1] == 0 && b[2] == 0xFE && b[3] == 0xFF) { enc = new UTF32Encoding(true, false, true); skip = 4; }
        else if (b.Length >= 4 && b[0] == 0xFF && b[1] == 0xFE && b[2] == 0 && b[3] == 0) { enc = new UTF32Encoding(false, false, true); skip = 4; }
        else if (b.Length >= 2 && b[0] == 0xFE && b[1] == 0xFF) { enc = new UnicodeEncoding(true, false, true); skip = 2; }
        else if (b.Length >= 2 && b[0] == 0xFF && b[1] == 0xFE) { enc = new UnicodeEncoding(false, false, true); skip = 2; }
        else if (b.Length >= 3 && b[0] == 0xEF && b[1] == 0xBB && b[2] == 0xBF) { enc = new UTF8Encoding(false, true); skip = 3; }
        else if (b.Length >= 4 && b[0] == 0 && b[1] == 0 && b[2] == 0 && b[3] != 0) enc = new UTF32Encoding(true, false, true);
        else if (b.Length >= 4 && b[0] != 0 && b[1] == 0 && b[2] == 0 && b[3] == 0) enc = new UTF32Encoding(false, false, true);
        else if (b.Length >= 2 && b[0] == 0 && b[1] != 0) enc = new UnicodeEncoding(true, false, true);
        else if (b.Length >= 2 && b[0] != 0 && b[1] == 0) enc = new UnicodeEncoding(false, false, true);
        else { enc = new UTF8Encoding(false, true); plainUtf8 = true; }
        try
        {
            return new Decoded
            {
                Text = enc.GetString(b, skip, b.Length - skip),
                Transformed = !plainUtf8,
                Encoding = enc,
                Bom = b[..skip],
            };
        }
        catch (DecoderFallbackException e)
        {
            return new Decoded
            {
                Failure = new Failure("encoding", "input is not valid " + enc.WebName + ": " + e.Message),
                Transformed = true,
                Encoding = enc,
            };
        }
    }
}
