#if SELFTEST
using System.Diagnostics.Tracing;
using System.Runtime;
using System.Text;
using OneYamlKit.Bench.Libs;
using SharpYaml.Syntax;

namespace OneYamlKit.Bench;

/// <summary>The harness's own tests. They exist only in the self-test build and run with
/// <c>selftest</c>; the kit's test run builds that assembly and checks that none fails. They prove that
/// the guard accepts a right answer and refuses a wrong one for every task, that the reference parsers
/// and the core-schema resolver read what the specification says, that edits are worked out exactly, and
/// that the memory figures are exact where they claim to be.</summary>
public static class SelfTests
{
    sealed class Failure(string message) : Exception(message);

    // Writing to a static keeps the optimiser from removing an allocation whose result is never read.
    static volatile byte[]? Sink;

    static void Check(bool ok, string what) { if (!ok) throw new Failure(what); }
    static void Equal<T>(T got, T want, string what)
    {
        if (!EqualityComparer<T>.Default.Equals(got, want)) throw new Failure($"{what}: got {got}, expected {want}");
    }

    static J P(string json) => J.Parse(Encoding.UTF8.GetBytes(json));

    static Workload Wl(string input, string expected, string? merged = null, string? edit = null, string? patch = null,
                       List<Op>? editOps = null) => new()
    {
        Id = "t",
        Input = Encoding.UTF8.GetBytes(input),
        InputText = input,
        Expected = merged == null ? [("spec", P(expected))] : [("spec", P(expected)), ("merged", P(merged))],
        EditExpected = edit == null ? null : Encoding.UTF8.GetBytes(edit),
        PatchExpected = patch == null ? null : Encoding.UTF8.GetBytes(patch),
        EditOps = editOps ?? [],
    };

    static J NoReferee(byte[] _) => throw new RefereeException(false, "unused");
    static Verdict Judge(BenchTask t, Workload w, Product p, FailedPatch? f = null, Func<byte[], J>? referee = null) =>
        Guard.Judge(t, w, () => p, () => f, referee ?? NoReferee);
    static Product V(string json) => new Product.Value(P(json));
    static Product B(string s) => new Product.Bytes(Encoding.UTF8.GetBytes(s));

    /// <summary>Collects the runtime's method-load events: for each method, the sequence of versions the JIT
    /// produced. In .NET 10 the tier is bits 7 to 9 of the event's flags: 3 is unoptimised, 6 is unoptimised
    /// and instrumented, 4 is optimised. A method that ends in 4 after a 6 was optimised with a profile.</summary>
    sealed class TierListener : EventListener
    {
        readonly Dictionary<ulong, (string Ns, string Seq)> _methods = new();

        protected override void OnEventSourceCreated(EventSource source)
        {
            if (source.Name == "Microsoft-Windows-DotNETRuntime") EnableEvents(source, EventLevel.Verbose, (EventKeywords)0x10);
        }

        protected override void OnEventWritten(EventWrittenEventArgs e)
        {
            if (e.EventName == null || !e.EventName.StartsWith("MethodLoadVerbose", StringComparison.Ordinal) || e.Payload == null || e.PayloadNames == null) return;
            ulong id = Convert.ToUInt64(e.Payload[e.PayloadNames.IndexOf("MethodID")]);
            var ns = e.Payload[e.PayloadNames.IndexOf("MethodNamespace")] as string ?? "";
            uint tier = (Convert.ToUInt32(e.Payload[e.PayloadNames.IndexOf("MethodFlags")]) >> 7) & 7;
            lock (_methods) _methods[id] = (ns, _methods.TryGetValue(id, out var old) ? old.Seq + tier : tier.ToString());
        }

        public int Loads(string ns)
        {
            lock (_methods) return _methods.Values.Where(m => m.Ns.StartsWith(ns, StringComparison.Ordinal)).Sum(m => m.Seq.Length);
        }

        public int Optimised(string ns)
        {
            lock (_methods) return _methods.Values.Count(m => m.Ns.StartsWith(ns, StringComparison.Ordinal) && m.Seq.EndsWith('4') && m.Seq.Contains('6'));
        }
    }

    static readonly List<(string, Action)> Tests =
    [
        ("an integer is not the float with the same digits, and \"1\" is not 1", () =>
        {
            var w = Wl("a: 1\n", """{"a":[1,2.5,null,"x"]}""");
            Equal(Judge(BenchTask.GenericLoad, w, V("""{"a":[1,2.5,null,"x"]}""")).Status, "ok", "equal values");
            foreach (var bad in new[] { """{"a":[1.0,2.5,null,"x"]}""", """{"a":["1",2.5,null,"x"]}""",
                                        """{"a":[1,2.5,null]}""", """{"a":[1,2.5,null,"x"],"b":1}""", """{"a":[1,2.5,null,"y"]}""" })
                Equal(Judge(BenchTask.GenericLoad, w, V(bad)).Status, "differs", bad);
        }),
        ("mapping order does not matter, sequence order does", () =>
        {
            var w = Wl("", """{"a":1,"b":[1,2]}""");
            Equal(Judge(BenchTask.GenericLoad, w, V("""{"b":[1,2],"a":1}""")).Status, "ok", "key order");
            Equal(Judge(BenchTask.GenericLoad, w, V("""{"a":1,"b":[2,1]}""")).Status, "differs", "item order");
        }),
        ("the first difference is named with a pointer", () =>
        {
            var d = J.FirstDiff(P("""{"a":{"b/c":[1,{"d":2}]}}"""), P("""{"a":{"b/c":[1,{"d":3}]}}"""));
            Check(d != null && d.StartsWith("at /a/b~1c/1/d:", StringComparison.Ordinal), "pointer: " + d);
            Check(J.FirstDiff(P("""{"a":1}"""), P("""{"a":1,"b":2}"""))!.StartsWith("missing /b", StringComparison.Ordinal), "missing key");
            Check(J.FirstDiff(P("""{"a":1,"b":2}"""), P("""{"a":1}"""))!.StartsWith("unexpected /b", StringComparison.Ordinal), "extra key");
        }),
        ("a refusal is its own verdict", () =>
        {
            var w = Wl("", "1");
            var v = Guard.Judge(BenchTask.GenericLoad, w, () => throw new Rejected("bad"), () => null, NoReferee);
            Equal(v, new Verdict.Rejects("bad"), "rejects");
        }),
        ("bytes must match exactly: a missing newline, CRLF and a byte order mark all differ", () =>
        {
            var w = Wl("a: 1\n", "1");
            Equal(Judge(BenchTask.UnchangedEmit, w, B("a: 1\n")).Status, "ok", "same");
            foreach (var bad in new[] { "a: 1", "a:  1\n", "a: 1\r\n", "\uFEFFa: 1\n" })
                Equal(Judge(BenchTask.UnchangedEmit, w, B(bad)).Status, "differs", bad);
        }),
        ("an edit has one accepted outcome", () =>
        {
            var w = Wl("a: 1\n", "1", edit: "a: 2\n");
            Equal(Judge(BenchTask.EditScript, w, B("a: 2\n")).Status, "ok", "exact");
            Equal(Judge(BenchTask.EditScript, w, B("a: '2'\n")).Status, "differs", "restyled");
            var none = Wl("a: 1\n", "1");
            Equal(Judge(BenchTask.EditScript, none, B("a: 2\n")).Status, "differs", "no expected output");
        }),
        ("a dump is judged by reading it back with the reference parsers", () =>
        {
            var w = Wl("", """{"a":1}""", merged: """{"a":2}""");
            Equal(Judge(BenchTask.Dump, w, B("x"), referee: _ => P("""{"a":1}""")).Status, "ok", "read back");
            var m = Judge(BenchTask.Dump, w, B("x"), referee: _ => P("""{"a":2}"""));
            Equal(m, new Verdict.Ok("merged"), "merged reading");
            var wrong = Judge(BenchTask.Dump, w, B("x"), referee: _ => P("""{"a":3}"""));
            Check(wrong is Verdict.Differs d && d.Why.StartsWith("read back:", StringComparison.Ordinal), "wrong reading");
            Equal(Judge(BenchTask.Dump, w, B("x"), referee: _ => throw new RefereeException(true, "they disagree")).Status, "unjudged", "disagree");
            var bad = Judge(BenchTask.Dump, w, B("x"), referee: _ => throw new RefereeException(false, "not yaml"));
            Check(bad is Verdict.Differs e && e.Why.StartsWith("the output does not parse", StringComparison.Ordinal), "unparseable");
        }),
        ("a document must hold the value and write the input back", () =>
        {
            var w = Wl("a: 1\n", """{"a":1}""");
            Equal(Judge(BenchTask.DocumentParse, w, new Product.Document(P("""{"a":1}"""), Encoding.UTF8.GetBytes("a: 1\n"))), new Verdict.Ok("spec"), "right");
            Equal(Judge(BenchTask.DocumentParse, w, new Product.Document(null, Encoding.UTF8.GetBytes("a: 1\n"))), new Verdict.Ok("bytes-only"), "bytes only");
            Equal(Judge(BenchTask.DocumentParse, w, new Product.Document(P("""{"a":2}"""), Encoding.UTF8.GetBytes("a: 1\n"))).Status, "differs", "wrong value");
            Equal(Judge(BenchTask.DocumentParse, w, new Product.Document(P("""{"a":1}"""), Encoding.UTF8.GetBytes("a:  1\n"))).Status, "differs", "wrong bytes");
            Equal(Judge(BenchTask.DocumentParse, w, new Product.Document(null, Encoding.UTF8.GetBytes("a:  1\n"))).Status, "differs", "bytes only, wrong bytes");
        }),
        ("an atomic patch must leave the document alone when a step fails", () =>
        {
            var w = Wl("a: 1\n", "1", patch: "a: 2\n");
            var input = Encoding.UTF8.GetBytes("a: 1\n");
            Equal(Judge(BenchTask.Patch, w, B("a: 2\n"), new FailedPatch(true, input)).Status, "ok", "atomic");
            Equal(Judge(BenchTask.Patch, w, B("a: 2\n"), new FailedPatch(false, input)).Status, "differs", "reported success");
            Equal(Judge(BenchTask.Patch, w, B("a: 2\n"), new FailedPatch(true, Encoding.UTF8.GetBytes("a: 9\n"))).Status, "differs", "changed");
            Equal(Judge(BenchTask.Patch, w, B("a: 2\n"), null).Status, "differs", "failing patch not checked");
            Equal(Judge(BenchTask.Patch, w, B("a: 3\n"), new FailedPatch(true, input)).Status, "differs", "wrong output");
        }),
        ("a product of the wrong kind is refused", () =>
            Equal(Judge(BenchTask.Dump, Wl("", "1"), V("1")).Status, "differs", "kind")),
        ("the core schema resolves plain scalars as the specification says", () =>
        {
            foreach (var s in new[] { "", "~", "null", "Null", "NULL" }) Check(Referee.Resolve(s) is J.Null, $"null {s}");
            foreach (var s in new[] { "true", "True", "TRUE" }) Check(Referee.Resolve(s) is J.Bool { V: true }, s);
            foreach (var s in new[] { "false", "False", "FALSE" }) Check(Referee.Resolve(s) is J.Bool { V: false }, s);
            Equal(Referee.Resolve("12").ToString(), "12", "int");
            Equal(Referee.Resolve("-0").ToString(), "0", "negative zero is an integer");
            Equal(Referee.Resolve("+5").ToString(), "5", "signed");
            Equal(Referee.Resolve("0o14").ToString(), "12", "octal");
            Equal(Referee.Resolve("0xFF").ToString(), "255", "hex");
            Equal(Referee.Resolve("12345678901234567890123").ToString(), "12345678901234567890123", "beyond 64 bits");
            foreach (var s in new[] { "1.5", ".5", "1.", "1e3", "-2.5E-3", "+.5" }) Check(Referee.Resolve(s) is J.Float, $"float {s}");
            Check(Referee.Resolve(".inf") is J.Float { V: double.PositiveInfinity }, ".inf");
            Check(Referee.Resolve("-.Inf") is J.Float { V: double.NegativeInfinity }, "-.Inf");
            Check(Referee.Resolve(".NaN") is J.Float f && double.IsNaN(f.V), ".NaN");
            // The 1.1 spellings and near misses stay strings.
            foreach (var s in new[] { "yes", "No", "on", "OFF", "0b11", "1_000", "0o", "0x", "1.2.3", "tRUE", "nUll", "012x", "1,000", "--1", ".", "-", "+" })
                Check(Referee.Resolve(s) is J.Str, $"string {s}");
        }),
        ("the reference parsers agree on plain YAML and read it by the core schema", () =>
        {
            var v = Referee.Read(Encoding.UTF8.GetBytes("a: [1, 2.5, x, null, true, '1', \"2\", !!str 3]\nb: &k {c: d}\ne: *k\n"));
            Equal(v.ToJson(), """{"a":[1,2.5,"x",null,true,"1","2","3"],"b":{"c":"d"},"e":{"c":"d"}}""", "value");
            var docs = Referee.Read(Encoding.UTF8.GetBytes("a: 1\n---\nb: 2\n"));
            Equal(docs.ToJson(), """[{"a":1},{"b":2}]""", "stream");
            Equal(Referee.Read(Encoding.UTF8.GetBytes("<<: {a: 1}\n")).ToJson(), """{"<<":{"a":1}}""", "merge key is an ordinary key");
        }),
        ("the reference parsers reject text that is not YAML, and say so", () =>
        {
            try { Referee.Read(Encoding.UTF8.GetBytes("a: [1, 2\n")); Check(false, "accepted"); }
            catch (RefereeException e) { Check(!e.Disagree, "both reject: " + e.Message); }
            try { Referee.Read([0xFF, 0xFE, 0xFD]); Check(false, "accepted bytes"); }
            catch (RefereeException e) { Check(!e.Disagree, "not UTF-8"); }
        }),
        ("a deeply nested value is read without recursion", () =>
        {
            var text = new string('[', 3000) + new string(']', 3000) + "\n";
            var v = Referee.Read(Encoding.UTF8.GetBytes(text));
            int depth = 0;
            for (J x = v; x is J.Arr { V.Count: 1 } a; x = a.V[0]) depth++;
            Equal(depth, 2999, "depth: every array holds one, the innermost none");
        }),
        ("pointers become keys and indexes by what they land on", () =>
        {
            var v = P("""{"a/b":[{"c":1}],"~":2,"7":{"8":9}}""");
            var s = Pointer.Segments(v, "/a~1b/0/c", false);
            Equal(s.Count, 3, "segments");
            Check(s[0] is { Key: "a/b" } && s[1] is { Key: null, Index: 0 } && s[2] is { Key: "c" }, "kinds");
            Equal(Pointer.Segments(v, "/~0", false)[0].Key, "~", "escape");
            Check(Pointer.Segments(v, "/7/8", false)[0].IsKey, "a number inside a mapping is a key");
            foreach (var bad in new[] { "/nope", "/a~1b/5", "/a~1b/x", "/~/z" })
            {
                try { Pointer.Segments(v, bad, false); Check(false, "accepted " + bad); }
                catch (InvalidOperationException) { }
            }
            Equal(Pointer.Segments(v, "/new", true).Count, 1, "a new key may be named by an add");
        }),
        ("median and spread are not moved by one slow sample", () =>
        {
            Equal(Timing.MedianSorted([1, 2, 9]), 2.0, "odd");
            Equal(Timing.MedianSorted([1, 2, 3, 10]), 2.5, "even");
            var t = new Timing(1, [10, 10, 10, 20, 30]);
            Equal(t.Median, 10.0, "median");
            Equal(t.Mad, 0.0, "mad");
        }),
        ("a batch builds one state per iteration and runs each exactly once", () =>
        {
            int built = 0, ran = 0;
            var r = new FnRunner<int, int>(() => ++built, s => { ran++; return s; }, _ => new Product.Bytes([]));
            var b = r.Prepare(7);
            Equal(built, 7, "states built");
            Equal(ran, 0, "nothing ran while preparing");
            b.Run();
            Equal(ran, 7, "tasks run");
        }),
        ("edits are worked out from pointers and applied to the exact bytes", () =>
        {
            string Apply(string input, string expected, params Op[] ops)
            {
                var tree = YamlSyntaxTree.Parse(input, new YamlSyntaxOptions { IncludeTrivia = true });
                try { return SharpYamlEdits.Apply(tree, ops).ToFullString(); }
                catch (Unsupported u) { throw new Failure($"{u.Message} (steps {string.Join(", ", ops.Select(o => o.Name + " " + o.Path))})"); }
            }
            Op Set(string path, J v) => new("replace", path, v);
            Op Del(string path) => new("remove", path, null);

            var input = "#c\na: 1  # keep\nb: \"old\"\nc: 'x'\nd: plain\ne: |\n  one\nf: [1, 2]\ng:\n  - x\n  - y\n  - z\nh:\n  k: 1\n  m: 2\n";
            const string value = """{"a":1,"b":"old","c":"x","d":"plain","e":"one\n","f":[1,2],"g":["x","y","z"],"h":{"k":1,"m":2}}""";
            Equal(Apply(input, value, Set("/a", J.Of(5))),
                  input.Replace("a: 1  # keep", "a: 5  # keep"), "a number keeps its comment");
            Equal(Apply(input, value, Set("/b", J.Of("new \"q\""))),
                  input.Replace("b: \"old\"", "b: \"new \\\"q\\\"\""), "double quotes stay");
            Equal(Apply(input, value, Set("/c", J.Of("it's"))),
                  input.Replace("c: 'x'", "c: 'it''s'"), "single quotes stay");
            Equal(Apply(input, value, Set("/d", J.Of("1.5"))),
                  input.Replace("d: plain", "d: '1.5'"), "a string that would read as a number is quoted");
            Equal(Apply(input, value, Set("/e", J.Of("two\nlines"))),
                  input.Replace("e: |\n  one\n", "e: |-\n  two\n  lines\n"), "a block scalar keeps its style and gets the chomping it needs");
            Equal(Apply(input, value, Del("/g/1")),
                  input.Replace("  - y\n", ""), "a sequence item goes with its dash");
            Equal(Apply(input, value, Del("/h/k"), Set("/a", J.Of(true))),
                  input.Replace("  k: 1\n", "").Replace("a: 1  # keep", "a: true  # keep"), "edits do not move each other");
            Equal(Apply(input, value, Del("/f")),
                  input.Replace("f: [1, 2]\n", ""), "a flow value goes with its key");
            // A sequence that is not indented under its key has no start or end token of its own.
            var flat = "ik:\n- x\n- y: 2\n  z: 3\nnext: 1\nempty:\nlast: &a !!str v\n\"q k\": old\nnest:\n  # a comment before the first key\n  k: 1\n  j: 2\n";
            const string flatValue = """{"ik":["x",{"y":2,"z":3}],"next":1,"empty":null,"last":"v","q k":"old","nest":{"k":1,"j":2}}""";
            Equal(Apply(flat, flatValue, Set("/ik/0", J.Of("w"))),
                  flat.Replace("- x\n", "- w\n"), "an item of a sequence with no indent");
            Equal(Apply(flat, flatValue, Set("/ik/1/z", J.Of(4))),
                  flat.Replace("z: 3", "z: 4"), "a mapping inside such a sequence");
            Equal(Apply(flat, flatValue, Set("/next", J.Of(9))),
                  flat.Replace("next: 1", "next: 9"), "the key after such a sequence is not part of it");
            Equal(Apply(flat, flatValue, Del("/ik/0")),
                  flat.Replace("- x\n", ""), "an item of such a sequence goes with its dash");
            Equal(Apply(flat, flatValue, Set("/last", J.Of("new"))),
                  flat.Replace("&a !!str v", "&a !!str new"), "an anchor and a tag stay");
            Equal(Apply(flat, flatValue, Set("/q k", J.Of("fresh"))),
                  flat.Replace("old", "fresh"), "a key in quotes is found by its value");
            Equal(Apply(flat, flatValue, Set("/nest/j", J.Of(5))),
                  flat.Replace("j: 2", "j: 5"), "a mapping that starts after a comment line");
            Equal(Apply(flat, flatValue, Del("/empty")),
                  flat.Replace("empty:\n", ""), "a key with no value is removed whole");
            try
            {
                SharpYamlEdits.Apply(YamlSyntaxTree.Parse(flat, new YamlSyntaxOptions { IncludeTrivia = true }), [Set("/empty", J.Of("x"))]);
                Check(false, "an empty value was replaced");
            }
            catch (Unsupported) { }
            try
            {
                SharpYamlEdits.Apply(YamlSyntaxTree.Parse("{a: 1, b: 2}\n", new YamlSyntaxOptions { IncludeTrivia = true }), [Del("/b")]);
                Check(false, "removal inside a flow mapping was prepared");
            }
            catch (Unsupported) { }
        }),
        ("the memory figures are exact where they claim to be", () =>
        {
            // Nothing is allocated: both figures are zero apart from the starting state.
            var idle = new FnRunner<int, int>(() => 0, s => s + 1, _ => new Product.Bytes([]));
            var u0 = Measure.Memory(idle);
            Equal(u0.AllocatedBytes, 0L, "an idle task allocates nothing");
            Check(u0.PeakBytes < 4096, "an idle task has no peak to speak of: " + u0.PeakBytes);
            // One megabyte, dropped at once: allocated is that, and the heap grew by that much.
            var alloc = new FnRunner<int, byte[]>(() => 0, _ => new byte[1 << 20], _ => new Product.Bytes([]));
            var u1 = Measure.Memory(alloc);
            Check(u1.AllocatedBytes is >= (1 << 20) and < (1 << 20) + 256, "allocated bytes: " + u1.AllocatedBytes);
            Check(u1.PeakBytes >= u1.AllocatedBytes, "peak at least what was allocated without a collection: " + u1.PeakBytes);
            // A starting state of two megabytes is part of the peak but not of what the task allocated.
            var held = new FnRunner<byte[], int>(() => new byte[2 << 20], s => s.Length, _ => new Product.Bytes([]));
            var u2 = Measure.Memory(held);
            Equal(u2.AllocatedBytes, 0L, "the state is not the task's allocation");
            Check(u2.PeakBytes is >= (2 << 20) and < (2 << 20) + 16384, "the state counts toward the peak: " + u2.PeakBytes);
            // The same cell measured twice agrees to the byte.
            Equal(Measure.Memory(alloc).AllocatedBytes, u1.AllocatedBytes, "repeatable");
        }),
        ("a runner may share one state only if the task leaves it alone", () =>
        {
            int built = 0;
            var honest = new FnRunner<int[], byte[]>(() => { built++; return [7]; }, s => [(byte)s[0]], b => new Product.Bytes(b), sharedState: true);
            honest.Check();
            var batch = honest.Prepare(5);
            Equal(built, 2, "one state for the check (both of its runs), one for the whole batch");
            batch.Run();
            var mutating = new FnRunner<int[], byte[]>(() => [0], s => [(byte)(++s[0])], b => new Product.Bytes(b), sharedState: true);
            try { mutating.Check(); Check(false, "a task that changes its state was allowed to share it"); }
            catch (InvalidOperationException e) { Check(e.Message.Contains("starting state"), e.Message); }
            var own = new FnRunner<int[], byte[]>(() => [0], s => [(byte)(++s[0])], b => new Product.Bytes(b));
            Equal(own.Check() is Product.Bytes, true, "a runner with a state per task may change it");
        }),
        ("a batch is never smaller than the sample target asks for, however slow its states are to build", () =>
        {
            // The states take about 2 ms to build and the task about 5 us. A budget on set-up time used to cut the
            // batch to a handful, which made the cost of one invocation the answer.
            var slow = new FnRunner<int, long>(
                () => { var t = System.Diagnostics.Stopwatch.StartNew(); while (t.Elapsed.TotalMilliseconds < 2) { } return 0; },
                _ => { long x = 0; for (int i = 0; i < 3000; i++) x += i; return x; }, _ => new Product.Bytes([]));
            Measure.ArtifactsPath = Path.Combine(Path.GetTempPath(), "oneyaml-bench-selftest-" + Environment.ProcessId);
            var cfg = Config.Quick with { WarmupMaxMs = 300, SettleWindowMs = 20 };
            var t = Measure.Time(slow, cfg);
            // Judged on the estimate the batch was sized from, not on the speed the samples then ran at: the JIT
            // keeps optimising for as long as it likes (on a slow or busy machine for longer), and the test config
            // here cuts the warm-up short. What the test is about is that the cost of building states does not
            // shrink the batch below what the target asks for.
            Check(t.ItersPerSample * t.CalibratedNs >= 0.9 * cfg.SampleTargetMs * 1e6,
                  $"a batch of {t.ItersPerSample} tasks of an estimated {t.CalibratedNs:F0} ns is shorter than the {cfg.SampleTargetMs} ms target");
            Equal(t.SamplesNs.Length, cfg.MinSamples, "samples are cut only down to the minimum");
        }),
        ("the peak is the starting state plus what the task requested, whatever the collector did before", () =>
        {
            // 300 MB of small objects, dropped as they are made: more than any young generation holds, so a
            // collection inside the run frees most of it, and a peak read from the heap would depend on when
            // the collection happened.
            var churn = new FnRunner<byte[], int>(() => new byte[1 << 20], s =>
            {
                int n = 0;
                for (int i = 0; i < 300_000; i++) { Sink = new byte[1000]; n++; }
                return n + s.Length;
            }, _ => new Product.Bytes([]));
            var a = Measure.Memory(churn);
            Equal(a.PeakBytes, a.StateBytes + a.AllocatedBytes, "peak is the state plus what was requested");
            Check(a.AllocatedBytes >= 300_000_000, "allocated bytes: " + a.AllocatedBytes);
            // Fill the young generation with other work, then measure again, and again after a full collection.
            for (int i = 0; i < 2000; i++) GC.KeepAlive(new byte[40_000]);
            var b = Measure.Memory(churn);
            GC.Collect();
            var c = Measure.Memory(churn);
            // What the task requested is exact; the state is a difference of two live-size readings and can move by a
            // few kilobytes with what else is alive.
            Equal(b.AllocatedBytes, a.AllocatedBytes, "requested after other allocation");
            Equal(c.AllocatedBytes, a.AllocatedBytes, "requested after a full collection");
            Check(Math.Abs(b.PeakBytes - a.PeakBytes) < a.PeakBytes / 100, $"peak after other allocation: {b.PeakBytes} against {a.PeakBytes}");
            Check(Math.Abs(c.PeakBytes - a.PeakBytes) < a.PeakBytes / 100, $"peak after a full collection: {c.PeakBytes} against {a.PeakBytes}");
        }),
        ("what a library keeps after a run is reported apart from the peak", () =>
        {
            var keep = new List<byte[]>();
            var keeper = new FnRunner<int, int>(() => 0, _ => { keep.Add(new byte[1 << 20]); return 1; }, _ => new Product.Bytes([]));
            var tidy = new FnRunner<int, byte[]>(() => 0, _ => new byte[1 << 20], _ => new Product.Bytes([]));
            // The figure comes with the memory pass: the live heap one collection after one run, less the live heap
            // just before it, with the starting state held (the definition every harness shares).
            long kept = Measure.Memory(keeper).RetainedBytes;
            Check(kept >= (1 << 20), "a buffer the library holds on to is retained: " + kept);
            long garbage = Measure.Memory(tidy).RetainedBytes;
            Check(garbage < 64 * 1024, "garbage is not retained: " + garbage);
            GC.KeepAlive(keep);
        }),
        ("the starting state is held in both readings, so it is not what the run retained", () =>
        {
            // A large state that the task only reads: it is live before and after, and the run leaves nothing.
            var big = new FnRunner<byte[], int>(() => new byte[8 << 20], s => s[0], _ => new Product.Bytes([]));
            long kept = Measure.Memory(big).RetainedBytes;
            Check(kept < 64 * 1024, "a state the run only reads is not retained: " + kept);
        }),
        ("a full run keeps at least thirty samples, and fewer are allowed only in quick mode", () =>
        {
            Check(Config.Full.MinSamples >= 30, "full-mode floor: " + Config.Full.MinSamples);
            Check(Config.Full.Samples >= Config.Full.MinSamples, "full-mode target: " + Config.Full.Samples);
            Check(Config.Quick.MinSamples < 30, "quick mode is the smoke tier");
        }),
        ("warming up under the full-mode policy reaches optimised, profiled code and the JIT then goes quiet", () =>
        {
            // The same function the harness calls before it times a cell, with the full-mode settings, on a real
            // library. A method that is called often goes through three versions: unoptimised, instrumented for
            // the profile, and optimised with the profile's help. The runtime reports each as a method load.
            var text = Encoding.UTF8.GetBytes(string.Concat(Enumerable.Range(0, 120).Select(i => $"key{i}: [1, 2.5, 'x']\nm{i}:\n  a: b\n  c: [true, null]\n")));
            var vyaml = new FnRunner<int, object?>(() => 0, _ => VYaml.Serialization.YamlSerializer.Deserialize<object>(new ReadOnlyMemory<byte>(text)), _ => new Product.Bytes([]));
            var sharp = new FnRunner<int, object?>(() => 0, _ => SharpYaml.YamlSerializer.Deserialize<object>(Encoding.UTF8.GetString(text)), _ => new Product.Bytes([]));
            using var tiers = new TierListener();
            // A policy that stops after 30 ms cannot get there: the runtime waits 100 ms before it even counts calls.
            var hasty = Measure.WarmUp(sharp, Config.Quick with { WarmupMaxMs = 30, SettleWindowMs = 0, SettleWindows = 0 }, 1000);
            Equal(hasty.Settled, true, "a policy with no window settles at once");
            System.Threading.Thread.Sleep(300);
            Equal(tiers.Optimised("SharpYaml"), 0, "optimised methods after a 30 ms warm-up");
            var full = Measure.WarmUp(vyaml, Config.Full, 1000);
            Check(full.Settled, $"the full-mode warm-up did not settle in {full.Ms:F0} ms");
            System.Threading.Thread.Sleep(300);
            int optimised = tiers.Optimised("VYaml");
            Check(optimised >= 5, $"methods that reached optimised code after instrumentation: {optimised}");
            // Quiet: another stretch of the same work makes the JIT produce no new version of any VYaml method.
            int loads = tiers.Loads("VYaml");
            var clock = System.Diagnostics.Stopwatch.StartNew();
            while (clock.ElapsedMilliseconds < 500) vyaml.Prepare(1).Run();
            System.Threading.Thread.Sleep(300);
            Equal(tiers.Loads("VYaml"), loads, "VYaml methods compiled after the warm-up");
        }),
        ("a timed cell takes the configured number of samples and reports time per task", () =>
        {
            var spin = new FnRunner<int, long>(() => 0, _ => { long x = 0; for (int i = 0; i < 20000; i++) x += i; return x; },
                _ => new Product.Bytes([]));
            var cfg = Config.Quick;
            Measure.ArtifactsPath = Path.Combine(Path.GetTempPath(), "oneyaml-bench-selftest-" + Environment.ProcessId);
            var t = Measure.Time(spin, cfg);
            Equal(t.SamplesNs.Length, cfg.Samples, "samples");
            Check(t.SamplesNs.SequenceEqual(t.SamplesNs.OrderBy(x => x)), "sorted");
            Check(t.Median > 1_000 && t.Median < 5_000_000, "20000 additions take microseconds, not " + t.Median + " ns");
            Check(t.ItersPerSample >= 1, "iterations per sample");
        }),
    ];

    public static int Run()
    {
        int failed = 0;
        foreach (var (name, test) in Tests)
        {
            try { test(); Console.WriteLine("ok   " + name); }
            catch (Exception e)
            {
                failed++;
                Console.WriteLine($"FAIL {name}: {e.GetType().Name}: {e.Message}");
            }
        }
        Console.WriteLine($"{Tests.Count - failed} of {Tests.Count} passed");
        return failed == 0 ? 0 : 1;
    }
}
#endif
