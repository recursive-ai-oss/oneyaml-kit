using System.Buffers;
using VYaml.Parser;

/// <summary>
/// What VYaml's event API leaves out, read from its public tokenizer: whether a scalar is plain,
/// quoted or a block scalar, whether a collection is written in flow or block form, and whether a
/// document has explicit start and end markers. The parser gives the structure and the values; the
/// tokenizer gives these three facts, in the order the structure appears. Putting the two together is
/// the adapter's work and is declared as emulated.
/// </summary>
sealed class TokenFacts
{
    public readonly List<char> Scalars = [];
    public readonly List<(bool IsMap, bool Flow)> Collections = [];
    public readonly List<(bool ExplicitStart, bool ExplicitEnd)> Documents = [];
    /// <summary>The stream declares %TAG handles, which the event API neither applies nor exposes.</summary>
    public bool HasTagDirective;
    /// <summary>The tokenizer stopped on its own error after reading these facts.</summary>
    public Exception? Failure;

    enum Ctx { BlockSeq, BlockMap, IndentlessSeq, FlowSeq, FlowSeqPair, FlowMap }

    public static TokenFacts Read(byte[] utf8)
    {
        var facts = new TokenFacts();
        var tokenizer = new Utf8YamlTokenizer(new ReadOnlySequence<byte>(utf8));
        var stack = new Stack<Ctx>();
        bool inDoc = false, explicitStart = false;
        bool seenValue = false; // in the current block mapping, a ':' has been seen
        var sawValue = new Stack<bool>();

        void Open(bool explicitMarker)
        {
            inDoc = true;
            explicitStart = explicitMarker;
        }
        void Close(bool explicitEnd)
        {
            if (!inDoc) return;
            facts.Documents.Add((explicitStart, explicitEnd));
            inDoc = false;
            stack.Clear();
            sawValue.Clear();
            seenValue = false;
        }
        void PushCollection(Ctx c, bool isMap, bool flow)
        {
            facts.Collections.Add((isMap, flow));
            stack.Push(c);
            sawValue.Push(seenValue);
            seenValue = false;
        }
        void Pop()
        {
            if (stack.Count > 0) stack.Pop();
            seenValue = sawValue.Count > 0 && sawValue.Pop();
        }

        try
        {
            while (tokenizer.Read())
            {
                var t = tokenizer.CurrentTokenType;
                switch (t)
                {
                    case TokenType.StreamStart or TokenType.VersionDirective: break;
                    case TokenType.TagDirective: facts.HasTagDirective = true; break;
                    case TokenType.StreamEnd: Close(false); break;
                    case TokenType.DocumentStart:
                        Close(false);
                        Open(true);
                        break;
                    case TokenType.DocumentEnd:
                        if (!inDoc) Open(false);
                        Close(true);
                        break;
                    default:
                        if (!inDoc) Open(false);
                        break;
                }
                switch (t)
                {
                    case TokenType.PlainScalar: facts.Scalars.Add(':'); break;
                    case TokenType.SingleQuotedScaler: facts.Scalars.Add('\''); break;
                    case TokenType.DoubleQuotedScaler: facts.Scalars.Add('"'); break;
                    case TokenType.LiteralScalar: facts.Scalars.Add('|'); break;
                    case TokenType.FoldedScalar: facts.Scalars.Add('>'); break;
                    case TokenType.BlockSequenceStart: PushCollection(Ctx.BlockSeq, false, false); break;
                    case TokenType.BlockMappingStart: PushCollection(Ctx.BlockMap, true, false); break;
                    case TokenType.FlowSequenceStart: PushCollection(Ctx.FlowSeq, false, true); break;
                    case TokenType.FlowMappingStart: PushCollection(Ctx.FlowMap, true, true); break;
                    case TokenType.FlowSequenceEnd or TokenType.FlowMappingEnd:
                        if (stack.Count > 0 && stack.Peek() == Ctx.FlowSeqPair) Pop();
                        Pop();
                        break;
                    case TokenType.BlockEnd:
                        if (stack.Count > 0 && stack.Peek() == Ctx.IndentlessSeq) Pop();
                        Pop();
                        break;
                    case TokenType.KeyStart:
                        if (stack.Count > 0 && stack.Peek() == Ctx.IndentlessSeq) Pop();
                        // A key directly inside a flow sequence is a single-pair mapping.
                        if (stack.Count > 0 && stack.Peek() == Ctx.FlowSeq) PushCollection(Ctx.FlowSeqPair, true, true);
                        seenValue = false;
                        break;
                    case TokenType.ValueStart:
                        if (stack.Count > 0 && stack.Peek() == Ctx.FlowSeq) PushCollection(Ctx.FlowSeqPair, true, true);
                        seenValue = true;
                        break;
                    case TokenType.FlowEntryStart:
                        if (stack.Count > 0 && stack.Peek() == Ctx.FlowSeqPair) Pop();
                        break;
                    case TokenType.BlockEntryStart:
                        // An entry directly under a mapping value, with no sequence start before it, is an
                        // indentless sequence.
                        if (stack.Count > 0 && stack.Peek() == Ctx.BlockMap && seenValue)
                            PushCollection(Ctx.IndentlessSeq, false, false);
                        break;
                }
            }
        }
        catch (Exception e)
        {
            facts.Failure = e;
            // The parser may already have reported the open document's start before the tokenizer gave up, so
            // what was read of it still counts.
            Close(false);
        }
        return facts;
    }
}
