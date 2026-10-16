using System.Runtime.ExceptionServices;
using System.Text;
using System.Text.Json;

namespace OneYamlKit.Adapter;

/// <summary>What a library adapter provides to the protocol loop.</summary>
public interface IAdapter
{
    string Name { get; }
    string Version { get; }
    string[] Capabilities { get; }
    /// <summary>Every kind of behaviour this adapter may supply itself (see the protocol document).</summary>
    string[] Emulates { get; }
    IReadOnlyDictionary<string, string> Info { get; }
    Outcome Run(Request request);

    /// <summary>True for the exceptions the library itself raises to report a problem with the input or
    /// the request (its own exception hierarchy). Anything else escaping a library call is a defect in
    /// the library, and the adapter answers `crash` for that case instead of an error.</summary>
    bool IsLibraryException(Exception e);
}

/// <summary>The library failed with an exception that is not part of its own error hierarchy.</summary>
public sealed class LibraryCrash(string message) : Exception(message);

/// <summary>The JSON Lines loop, and the threads library code runs on.</summary>
public static class Host
{
    /// <summary>Stack size for library calls: the Linux default for a program's main thread. A library
    /// that recurses on nesting depth overflows it at the depth it would overflow in an ordinary
    /// program, and the process dies; the kit sees that as a crash of that one case.</summary>
    public const int LibraryStackBytes = 8 * 1024 * 1024;

    /// <summary>Stack size for the adapter's own conversions, large enough that the adapter is never
    /// what runs out of stack before the library does.</summary>
    public const int ConversionStackBytes = 512 * 1024 * 1024;

    static IAdapter? _adapter;

    /// <summary>Returns <paramref name="e"/> when the library raised it on purpose; throws <see cref="LibraryCrash"/>
    /// when it is anything else (a null dereference, an invalid-operation error, a failed cast inside the
    /// library). A library that fails on an input has not rejected it and must not be credited with a
    /// rejection; the crash travels up to the request loop, which answers it and keeps serving.</summary>
    public static Exception Own(Exception e)
    {
        if (e is LibraryCrash) throw e;
        if (_adapter == null || _adapter.IsLibraryException(e)) return e;
        throw new LibraryCrash(e.GetType().FullName + ": " + Trim(e.Message));
    }

    public static T OnStack<T>(int stackBytes, Func<T> work)
    {
        T result = default!;
        ExceptionDispatchInfo? failure = null;
        var t = new Thread(() =>
        {
            try { result = work(); }
            catch (Exception e) { failure = ExceptionDispatchInfo.Capture(e); }
        }, stackBytes);
        t.Start();
        t.Join();
        failure?.Throw();
        return result;
    }

    /// <summary>Runs library code on a thread with <see cref="LibraryStackBytes"/>.</summary>
    public static T Library<T>(Func<T> work) => OnStack(LibraryStackBytes, () =>
    {
        try { return work(); }
        catch (Exception e) { throw Own(e); }
    });

    /// <summary>Runs the adapter's own conversion code on a thread with a very large stack.</summary>
    public static T Convert<T>(Func<T> work) => OnStack(ConversionStackBytes, work);

    public static int Serve(IAdapter adapter)
    {
        _adapter = adapter;
        var input = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false), false, 1 << 20);
        using var output = Console.OpenStandardOutput();
        string? line;
        while ((line = input.ReadLine()) != null)
        {
            if (line.Length == 0) continue;
            string type;
            JsonDocument doc;
            try
            {
                doc = JsonDocument.Parse(line, new JsonDocumentOptions { MaxDepth = 1024 });
                type = doc.RootElement.GetProperty("type").GetString() ?? "";
            }
            catch (Exception) { return 2; }
            using (doc)
            {
                if (type == "bye") return 0;
                byte[]? reply = type switch
                {
                    "hello" => Hello(adapter),
                    "run" => Run(adapter, doc.RootElement),
                    _ => null,
                };
                if (reply == null) continue;
                output.Write(reply);
                output.WriteByte((byte)'\n');
                output.Flush();
            }
        }
        return 0;
    }

    static byte[] Hello(IAdapter a)
    {
        using var ms = new MemoryStream();
        using (var w = new Utf8JsonWriter(ms))
        {
            w.WriteStartObject();
            w.WriteString("type", "hello");
            w.WriteNumber("protocol", 2);
            w.WriteString("name", a.Name);
            w.WriteString("version", a.Version);
            w.WriteString("language", "csharp");
            w.WriteStartArray("capabilities");
            foreach (var c in a.Capabilities) w.WriteStringValue(c);
            w.WriteEndArray();
            w.WriteStartArray("emulates");
            foreach (var c in a.Emulates) w.WriteStringValue(c);
            w.WriteEndArray();
            w.WriteStartObject("info");
            foreach (var (k, v) in a.Info) w.WriteString(k, v);
            w.WriteEndObject();
            w.WriteEndObject();
        }
        return ms.ToArray();
    }

    static byte[] Run(IAdapter a, JsonElement req)
    {
        var id = req.TryGetProperty("id", out var idp) ? idp.GetString() ?? "" : "";
        Outcome outcome;
        try
        {
            var op = req.GetProperty("op").GetString() ?? "";
            var bytes = System.Convert.FromBase64String(req.GetProperty("input").GetString() ?? "");
            var args = req.TryGetProperty("args", out var ap) ? ap.Clone() : default;
            outcome = a.Run(new Request { Id = id, Op = op, Input = bytes, Args = args });
        }
        catch (LibraryCrash c)
        {
            outcome = Outcome.Crash(c.Message);
        }
        catch (Exception e)
        {
            // Anything not mapped by the adapter is the adapter's own failure to answer, not a
            // verdict on the library.
            outcome = Outcome.Unsupported($"adapter could not answer: {e.GetType().Name}: {Trim(e.Message)}");
        }
        return Respond(id, outcome);
    }

    internal static string Trim(string s) => s.Length <= 300 ? s : s[..300];

    static byte[] Respond(string id, Outcome o)
    {
        using var ms = new MemoryStream();
        using (var w = new Utf8JsonWriter(ms))
        {
            w.WriteStartObject();
            w.WriteString("id", id);
            switch (o.Kind)
            {
                case Outcome.Kinds.Events:
                    w.WriteString("status", "ok");
                    WriteLines(w, "events", o.Events!);
                    break;
                case Outcome.Kinds.Value:
                    w.WriteString("status", "ok");
                    w.WritePropertyName("value");
                    w.WriteRawValue(o.ValueJson!, skipInputValidation: true);
                    break;
                case Outcome.Kinds.Output:
                    w.WriteString("status", "ok");
                    w.WriteString("output", System.Convert.ToBase64String(o.Output!));
                    break;
                case Outcome.Kinds.Error:
                    w.WriteString("status", "error");
                    w.WriteStartObject("error");
                    w.WriteString("category", o.Failure!.Category);
                    w.WriteString("message", o.Failure.Message);
                    if (o.Failure.Line is int l) w.WriteNumber("line", l);
                    if (o.Failure.Column is int c) w.WriteNumber("column", c);
                    w.WriteEndObject();
                    if (o.Events != null) WriteLines(w, "events", o.Events);
                    if (o.Output != null) w.WriteString("output", System.Convert.ToBase64String(o.Output));
                    break;
                case Outcome.Kinds.Crash:
                    w.WriteString("status", "crash");
                    w.WriteString("detail", "library crash: " + o.Reason);
                    break;
                case Outcome.Kinds.Accepted:
                    w.WriteString("status", "accepted");
                    break;
                default:
                    w.WriteString("status", "unsupported");
                    w.WriteString("reason", o.Reason ?? "");
                    break;
            }
            if (o.Emulated.Count > 0)
            {
                w.WriteStartArray("emulated");
                foreach (var e in o.Emulated) w.WriteStringValue(e);
                w.WriteEndArray();
            }
            w.WriteEndObject();
        }
        return ms.ToArray();
    }

    static void WriteLines(Utf8JsonWriter w, string name, List<string> lines)
    {
        w.WriteStartArray(name);
        foreach (var l in lines) w.WriteStringValue(l);
        w.WriteEndArray();
    }
}
