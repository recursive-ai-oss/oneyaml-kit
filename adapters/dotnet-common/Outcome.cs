using System.Text.Json;

namespace OneYamlKit.Adapter;

/// <summary>A failure in the kit's terms: a stable category and, where known, a 1-based position.</summary>
public sealed record Failure(string Category, string Message, int? Line = null, int? Column = null);

/// <summary>One request from the kit.</summary>
public sealed class Request
{
    public required string Id { get; init; }
    public required string Op { get; init; }
    public required byte[] Input { get; init; }
    public required JsonElement Args { get; init; }

    public string Profile =>
        Args.ValueKind == JsonValueKind.Object && Args.TryGetProperty("profile", out var p) &&
        p.ValueKind == JsonValueKind.String ? p.GetString()! : "core";
}

/// <summary>What an adapter answers. Behaviour the adapter supplied itself is named in
/// <see cref="Emulated"/>, so the kit never credits it to the library.</summary>
public sealed class Outcome
{
    public enum Kinds { Events, Value, Output, Error, Unsupported, Accepted, Crash }

    public Kinds Kind { get; private init; }
    public List<string>? Events { get; private init; }
    public string? ValueJson { get; private init; }
    public byte[]? Output { get; private init; }
    public Failure? Failure { get; private init; }
    public string? Reason { get; private init; }
    public SortedSet<string> Emulated { get; } = new(StringComparer.Ordinal);

    public static Outcome EventsOk(List<string> lines) => new() { Kind = Kinds.Events, Events = lines };
    public static Outcome ValueOk(string json) => new() { Kind = Kinds.Value, ValueJson = json };
    public static Outcome OutputOk(byte[] bytes) => new() { Kind = Kinds.Output, Output = bytes };

    /// <summary>An error. <paramref name="events"/> are the lines produced before it (events
    /// operations); <paramref name="output"/> is the document as it would now be written (patches).</summary>
    public static Outcome Error(Failure f, List<string>? events = null, byte[]? output = null) =>
        new() { Kind = Kinds.Error, Failure = f, Events = events, Output = output };

    /// <summary>The library took the input and the adapter does not describe the result.</summary>
    public static Outcome Accepted() => new() { Kind = Kinds.Accepted };

    /// <summary>The library failed with an exception outside its own error hierarchy.</summary>
    public static Outcome Crash(string detail) => new() { Kind = Kinds.Crash, Reason = detail };

    public static Outcome Unsupported(string reason) => new() { Kind = Kinds.Unsupported, Reason = reason };

    public Outcome Emulating(params string[] names)
    {
        foreach (var n in names) Emulated.Add(n);
        return this;
    }
}
