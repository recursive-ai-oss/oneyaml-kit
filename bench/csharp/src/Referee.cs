using System.Globalization;
using System.Numerics;
using System.Text;
using System.Text.RegularExpressions;
using YamlDotNet.Core;
using YamlDotNet.Core.Events;

namespace OneYamlKit.Bench;

/// <summary>Reads bytes with two independent parsers (YamlDotNet's and SharpYaml's event parsers) and one
/// resolver of the harness's own, so the text a library dumped is judged the way the specification reads it
/// (the YAML 1.2 core schema) and not by either library's idea of a type. The parsers must agree; when they
/// do not, the output cannot be judged and that is said as such instead of blaming the library.</summary>
public static class Referee
{
    /// <summary>One event in the form both parsers are reduced to.</summary>
    public enum K { DocStart, DocEnd, MapStart, MapEnd, SeqStart, SeqEnd, Scalar, Alias }

    public readonly record struct Ev(K Kind, string? Anchor, string? Tag, string Value, bool Plain);

    public static J Read(byte[] bytes)
    {
        string text;
        try { text = new UTF8Encoding(false, true).GetString(bytes); }
        catch (Exception e) { throw new RefereeException(false, $"not UTF-8: {e.Message}"); }
        J? a = null, b = null;
        string? ea = null, eb = null;
        try { a = Build(YamlDotNetEvents(text)); } catch (Exception e) { ea = e.Message; }
        try { b = Build(SharpYamlEvents(text)); } catch (Exception e) { eb = e.Message; }
        if (a != null && b != null)
        {
            var d = J.FirstDiff(a, b);
            if (d == null) return a;
            throw new RefereeException(true, $"the two reference parsers disagree {d}");
        }
        if (a == null && b == null) throw new RefereeException(false, $"YamlDotNet: {J.Clip(ea!, 60)}; SharpYaml: {J.Clip(eb!, 60)}");
        throw new RefereeException(true, a == null
            ? $"YamlDotNet rejects it, SharpYaml reads it: {J.Clip(ea!, 80)}"
            : $"SharpYaml rejects it, YamlDotNet reads it: {J.Clip(eb!, 80)}");
    }

    // ---- the two parsers ---------------------------------------------------------------

    static string? Anchor(AnchorName a) => a.IsEmpty ? null : a.Value;

    static string? Nz(string? s) => string.IsNullOrEmpty(s) ? null : s;

    public static IEnumerable<Ev> YamlDotNetEvents(string text)
    {
        var p = new Parser(new StringReader(text));
        while (p.MoveNext())
        {
            switch (p.Current)
            {
                case DocumentStart: yield return new Ev(K.DocStart, null, null, "", false); break;
                case DocumentEnd: yield return new Ev(K.DocEnd, null, null, "", false); break;
                case MappingStart m: yield return new Ev(K.MapStart, Anchor(m.Anchor), null, "", false); break;
                case MappingEnd: yield return new Ev(K.MapEnd, null, null, "", false); break;
                case SequenceStart s: yield return new Ev(K.SeqStart, Anchor(s.Anchor), null, "", false); break;
                case SequenceEnd: yield return new Ev(K.SeqEnd, null, null, "", false); break;
                case Scalar sc:
                    yield return new Ev(K.Scalar, Anchor(sc.Anchor), sc.Tag.IsEmpty ? null : sc.Tag.Value, sc.Value,
                                        sc.Style == ScalarStyle.Plain);
                    break;
                case AnchorAlias al: yield return new Ev(K.Alias, al.Value.Value, null, "", false); break;
            }
        }
    }

    public static IEnumerable<Ev> SharpYamlEvents(string text)
    {
        var p = SharpYaml.Parser.CreateParser(new StringReader(text), 1_000_000);
        while (p.MoveNext())
        {
            switch (p.Current)
            {
                case SharpYaml.Events.DocumentStart: yield return new Ev(K.DocStart, null, null, "", false); break;
                case SharpYaml.Events.DocumentEnd: yield return new Ev(K.DocEnd, null, null, "", false); break;
                case SharpYaml.Events.MappingStart m: yield return new Ev(K.MapStart, Nz(m.Anchor), null, "", false); break;
                case SharpYaml.Events.MappingEnd: yield return new Ev(K.MapEnd, null, null, "", false); break;
                case SharpYaml.Events.SequenceStart s: yield return new Ev(K.SeqStart, Nz(s.Anchor), null, "", false); break;
                case SharpYaml.Events.SequenceEnd: yield return new Ev(K.SeqEnd, null, null, "", false); break;
                case SharpYaml.Events.Scalar sc:
                    yield return new Ev(K.Scalar, Nz(sc.Anchor), Nz(sc.Tag), sc.Value, sc.Style == SharpYaml.ScalarStyle.Plain);
                    break;
                case SharpYaml.Events.AnchorAlias al: yield return new Ev(K.Alias, al.Value, null, "", false); break;
            }
        }
    }

    // ---- the YAML 1.2 core schema ------------------------------------------------------

    static readonly Regex IntRe = new(@"^[-+]?[0-9]+$", RegexOptions.CultureInvariant);
    static readonly Regex OctRe = new(@"^0o[0-7]+$", RegexOptions.CultureInvariant);
    static readonly Regex HexRe = new(@"^0x[0-9a-fA-F]+$", RegexOptions.CultureInvariant);
    static readonly Regex FloatRe = new(@"^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?$", RegexOptions.CultureInvariant);
    static readonly Regex InfRe = new(@"^[-+]?\.(inf|Inf|INF)$", RegexOptions.CultureInvariant);
    static readonly Regex NanRe = new(@"^\.(nan|NaN|NAN)$", RegexOptions.CultureInvariant);

    /// <summary>Resolves a plain scalar by the core schema (YAML 1.2.2 section 10.3.2).</summary>
    public static J Resolve(string s)
    {
        if (s.Length == 0 || s is "~" or "null" or "Null" or "NULL") return J.Null.I;
        if (s is "true" or "True" or "TRUE") return new J.Bool(true);
        if (s is "false" or "False" or "FALSE") return new J.Bool(false);
        if (IntRe.IsMatch(s)) return new J.Int(BigInteger.Parse(s, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture));
        if (OctRe.IsMatch(s))
        {
            BigInteger v = 0;
            foreach (var c in s.AsSpan(2)) v = v * 8 + (c - '0');
            return new J.Int(v);
        }
        if (HexRe.IsMatch(s)) return new J.Int(BigInteger.Parse("0" + s[2..], NumberStyles.AllowHexSpecifier, CultureInfo.InvariantCulture));
        if (FloatRe.IsMatch(s)) return new J.Float(double.Parse(s, NumberStyles.Float, CultureInfo.InvariantCulture));
        if (InfRe.IsMatch(s)) return new J.Float(s[0] == '-' ? double.NegativeInfinity : double.PositiveInfinity);
        if (NanRe.IsMatch(s)) return new J.Float(double.NaN);
        return new J.Str(s);
    }

    // ---- building the tree -------------------------------------------------------------

    sealed class Frame(bool isMap, string? anchor)
    {
        public bool IsMap { get; } = isMap;
        public string? Anchor { get; } = anchor;
        public List<J> Items { get; } = [];
    }

    /// <summary>Builds the documents of an event stream without recursion: the value itself for one
    /// document, otherwise an array.</summary>
    public static J Build(IEnumerable<Ev> events)
    {
        var docs = new List<J>();
        var stack = new Stack<Frame>();
        var anchors = new Dictionary<string, J>(StringComparer.Ordinal);
        J? current = null;

        void Put(J v, string? anchor)
        {
            if (anchor != null) anchors[anchor] = v;
            if (stack.Count == 0) current = v; else stack.Peek().Items.Add(v);
        }

        foreach (var e in events)
        {
            switch (e.Kind)
            {
                case K.DocStart: current = null; break;
                case K.DocEnd: docs.Add(current ?? J.Null.I); current = null; break;
                case K.MapStart: stack.Push(new Frame(true, e.Anchor)); break;
                case K.SeqStart: stack.Push(new Frame(false, e.Anchor)); break;
                case K.SeqEnd:
                    {
                        var f = stack.Pop();
                        Put(new J.Arr(f.Items), f.Anchor);
                        break;
                    }
                case K.MapEnd:
                    {
                        var f = stack.Pop();
                        var o = new J.Obj();
                        for (int i = 0; i + 1 < f.Items.Count; i += 2) o.Put(J.KeyText(f.Items[i]), f.Items[i + 1]);
                        Put(o, f.Anchor);
                        break;
                    }
                case K.Alias:
                    if (!anchors.TryGetValue(e.Anchor!, out var target)) throw new InvalidDataException($"unknown alias *{e.Anchor}");
                    Put(target, null);
                    break;
                case K.Scalar:
                    {
                        J v;
                        if (e.Tag == "tag:yaml.org,2002:str") v = new J.Str(e.Value);
                        else if (!e.Plain) v = new J.Str(e.Value);
                        else v = Resolve(e.Value);
                        Put(v, e.Anchor);
                        break;
                    }
            }
        }
        if (stack.Count != 0) throw new InvalidDataException("the event stream ended inside a collection");
        return docs.Count == 1 ? docs[0] : new J.Arr(docs);
    }
}
