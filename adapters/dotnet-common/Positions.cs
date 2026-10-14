namespace OneYamlKit.Adapter;

/// <summary>The kit counts columns in characters (Unicode scalar values) of the decoded text. YamlDotNet and
/// SharpYaml count UTF-16 code units, so a character outside the Basic Multilingual Plane makes every later
/// column on its line one too high. This converts a reported column back, using the library's own line.</summary>
public static class Positions
{
    /// <param name="text">the decoded text the library parsed</param>
    /// <param name="index">the library's offset of the error, in UTF-16 code units</param>
    /// <param name="column">the library's column for it, 1-based, in UTF-16 code units</param>
    public static int CharColumn(string text, long index, long column)
    {
        long lineStart = index - (column - 1);
        if (lineStart < 0 || index > text.Length) return (int)column;
        int pairs = 0;
        for (long i = lineStart; i < index; i++)
            if (char.IsHighSurrogate(text[(int)i]) && i + 1 < text.Length && char.IsLowSurrogate(text[(int)i + 1])) pairs++;
        return (int)column - pairs;
    }
}
