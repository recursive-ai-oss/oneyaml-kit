namespace OneYamlKit.Bench;

/// <summary>The result of judging one library's answer to one task.</summary>
public abstract record Verdict
{
    public sealed record Ok(string Variant) : Verdict;
    /// <summary>Produced an answer that is not the expected one.</summary>
    public sealed record Differs(string Why) : Verdict;
    /// <summary>Refused an input the specification accepts.</summary>
    public sealed record Rejects(string Why) : Verdict;
    /// <summary>The harness could not decide: its two reference parsers disagree about the output.
    /// Not charged to the library under test.</summary>
    public sealed record Unjudged(string Why) : Verdict;

    public string Status => this switch
    {
        Ok => "ok",
        Differs => "differs",
        Rejects => "rejects",
        _ => "unjudged",
    };

    public string Detail => this switch
    {
        Differs d => d.Why,
        Rejects r => r.Why,
        Unjudged u => u.Why,
        _ => "",
    };
}

/// <summary>Why the referees gave no single reading of some text.</summary>
public sealed class RefereeException(bool disagree, string message) : Exception(message)
{
    /// <summary>True when the reference parsers read it differently (or only one accepts it); false
    /// when none accepts it, so the text is not YAML.</summary>
    public bool Disagree { get; } = disagree;
}

/// <summary>The correctness guard: judges what a library produced before anything is timed.
/// Comparison is complete: the whole value, every output byte.</summary>
public static class Guard
{
    public static string Clip(string s) => J.Clip(s);

    public static string? BytesDiff(byte[] got, byte[] want)
    {
        if (got.AsSpan().SequenceEqual(want)) return null;
        int n = Math.Min(got.Length, want.Length);
        int at = 0;
        while (at < n && got[at] == want[at]) at++;
        string Ctx(byte[] b)
        {
            int lo = Math.Max(0, at - 12), hi = Math.Min(b.Length, at + 24);
            var s = System.Text.Encoding.UTF8.GetString(b, lo, hi - lo);
            return Clip(System.Text.Json.JsonSerializer.Serialize(s, J.Text));
        }
        return $"byte {at}: got {got.Length} bytes {Ctx(got)}, expected {want.Length} bytes {Ctx(want)}";
    }

    static Verdict MatchValue(Workload w, J got)
    {
        string? first = null;
        foreach (var (name, want) in w.Expected)
        {
            var d = J.FirstDiff(got, want);
            if (d == null) return new Verdict.Ok(name);
            first ??= d;
        }
        return new Verdict.Differs(first ?? "");
    }

    static Verdict MatchBytes(byte[] got, byte[]? want, string what)
    {
        if (want == null) return new Verdict.Differs($"workload has no expected {what}");
        var d = BytesDiff(got, want);
        return d == null ? new Verdict.Ok("spec") : new Verdict.Differs(d);
    }

    /// <summary>Judges one library's answer. <paramref name="referee"/> parses bytes to the common form with
    /// a fixed pair of independent parsers; a dumped document is accepted only if both read it back as the
    /// expected value.</summary>
    public static Verdict Judge(BenchTask task, Workload w, Func<Product> answer, Func<FailedPatch?> failing,
                                Func<byte[], J> referee)
    {
        Product product;
        try { product = answer(); }
        catch (Rejected r) { return new Verdict.Rejects(Clip(r.Message)); }

        switch (task, product)
        {
            case (BenchTask.TypedLoad or BenchTask.GenericLoad, Product.Value v):
                return MatchValue(w, v.Tree);

            case (BenchTask.Dump, Product.Bytes b):
                {
                    J read;
                    try { read = referee(b.Data); }
                    catch (RefereeException e)
                    {
                        return e.Disagree ? new Verdict.Unjudged(Clip(e.Message))
                                          : new Verdict.Differs($"the output does not parse: {Clip(e.Message)}");
                    }
                    var m = MatchValue(w, read);
                    return m is Verdict.Differs d ? new Verdict.Differs($"read back: {d.Why}") : m;
                }

            case (BenchTask.DocumentParse, Product.Document doc):
                {
                    var byValue = doc.Tree != null ? MatchValue(w, doc.Tree) : new Verdict.Ok("bytes-only");
                    var d = BytesDiff(doc.Emitted, w.Input);
                    if (byValue is Verdict.Ok && d != null) return new Verdict.Differs($"does not write the input back: {d}");
                    return byValue;
                }

            case (BenchTask.UnchangedEmit, Product.Bytes b):
                {
                    var d = BytesDiff(b.Data, w.Input);
                    return d == null ? new Verdict.Ok("spec") : new Verdict.Differs(d);
                }

            case (BenchTask.EditScript, Product.Bytes b):
                return MatchBytes(b.Data, w.EditExpected, "edit output");

            case (BenchTask.Patch, Product.Bytes b):
                {
                    var ok = MatchBytes(b.Data, w.PatchExpected, "patch output");
                    if (ok is not Verdict.Ok) return ok;
                    FailedPatch? f;
                    try { f = failing(); }
                    catch (Rejected r) { return new Verdict.Differs($"the failing patch was not checked: {Clip(r.Message)}"); }
                    if (f == null) return new Verdict.Differs("the failing patch was not checked");
                    if (!f.Errored) return new Verdict.Differs("a patch with a failing step reported success");
                    var d = BytesDiff(f.Bytes, w.Input);
                    return d != null ? new Verdict.Differs($"a failing patch changed the document: {d}") : ok;
                }

            default:
                return new Verdict.Differs($"the runner for {task.Id()} returned the wrong kind of product");
        }
    }
}
