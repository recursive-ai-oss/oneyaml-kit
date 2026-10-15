"""Reading a written document back, to judge a library's dump.

A `dump` case gives a value; the library writes YAML text for it; this module reads the text
and says whether it means that value. The reader is the kit's own, small and independent of
every library, because a library that judges its own output agrees with its own mistakes.

It reads the YAML that a writer has reason to produce, and refuses the rest with a ReadError
that names the construct: block and flow collections, plain, quoted and block scalars, `? `
keys, comments, one document with optional `---` and `...`, and the standard `!!` tags for
the core types. It has no anchors, aliases, custom tags or tag directives: a written value
has no shared nodes, so those would be the library's doing.

Text is judged under three readings, in this order of strictness:

* core: the YAML 1.2 Core schema (null, bool, int, float resolution of plain scalars);
* yaml11: the YAML 1.1 types a plain scalar can take (yes/no/on/off booleans, `0755` octal,
  `1_000` and `1:30` integers, floats with a point and a signed exponent, timestamps, `<<`
  and `=`);
* common: yaml11 plus the number forms that widely used readers accept beyond either schema
  (a `0x`/`0o`/`0b` prefix with a sign before or after it, a number with two or more signs, a
  number once every `_` is removed).

The first is the specification. The other two are the family's writing convention: a string
is written plain only when it reads as that same string under YAML 1.1 and under the listed
number forms that some common readers accept.

Syntax limits that a lenient reader would let through are checked here: an implicit key is on
one line and at most 1024 characters, and every character is printable or escaped.
"""

from __future__ import annotations

import re

from . import kitjson

MAX_IMPLICIT_KEY = 1024
MAX_DEPTH = 200
CORE, YAML11, COMMON = "core", "yaml11", "common"


class ReadError(Exception):
    """The text is not YAML the reader accepts (or not YAML at all)."""


# --------------------------------------------------------------------------
# Nodes


class Scalar:
    __slots__ = ("text", "style", "tag", "implicit_empty")

    def __init__(self, text, style="plain", tag=None, implicit_empty=False):
        self.text, self.style, self.tag, self.implicit_empty = text, style, tag, implicit_empty


class Seq:
    __slots__ = ("items", "tag")

    def __init__(self, items, tag=None):
        self.items, self.tag = items, tag


class Map:
    __slots__ = ("pairs", "tag")

    def __init__(self, pairs, tag=None):
        self.pairs, self.tag = pairs, tag


# --------------------------------------------------------------------------
# Characters

_PRINTABLE_EXTRA = {0x9, 0xA, 0xD, 0x85}


def _printable(ch: str) -> bool:
    o = ord(ch)
    return (o in _PRINTABLE_EXTRA or 0x20 <= o <= 0x7E or 0xA0 <= o <= 0xD7FF
            or 0xE000 <= o <= 0xFFFD or 0x10000 <= o <= 0x10FFFF)


_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f",
            "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85",
            "_": "\xa0", "L": "\u2028", "P": "\u2029"}
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}
_KNOWN_TAGS = ("str", "int", "float", "bool", "null", "map", "seq")
_WS = " \t"
_FLOW = ",[]{}"


def _is_ws_or_end(ch: str) -> bool:
    return ch == "" or ch in " \t\n"


# --------------------------------------------------------------------------
# Parser


class Parser:
    def __init__(self, text: str, allow_empty: bool = True):
        for ch in text:
            if not _printable(ch):
                raise ReadError(f"the character U+{ord(ch):04X} is written raw; it must be escaped")
        # Line breaks are normalised first: a bare CR is a line break, as the specification says.
        self.s = self._without_prefix_marks(text.replace("\r\n", "\n").replace("\r", "\n"))
        self.allow_empty = allow_empty
        # Where quoted scalars lie: a byte order mark is allowed inside them and nowhere else.
        self.quoted_spans: list[tuple[int, int]] = []
        self.prop_start = 0
        self.n = len(self.s)
        self.i = 0
        self.depth = 0

    @staticmethod
    def _without_prefix_marks(s: str) -> str:
        """Drops the byte order marks that a document prefix may hold: one at the start of a line
        before any content of a document, at the start of the stream or after a `...` line
        (l-document-prefix). Anywhere else a mark is checked after parsing."""
        if "\ufeff" not in s:
            return s
        out, prefix = [], True
        for line in s.split("\n"):
            body = line[1:] if prefix and line.startswith("\ufeff") else line
            if prefix:
                text = body.strip(" \t")
                if not (text == "" or text[0] in "#%" or body.rstrip(" \t") == "..."):
                    prefix = False
            elif body.rstrip(" \t") == "..." and line == body:
                prefix = True
            out.append(body)
        return "\n".join(out)

    # -- low level
    def peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.s[j] if j < self.n else ""

    def col(self, i: int | None = None) -> int:
        i = self.i if i is None else i
        return i - (self.s.rfind("\n", 0, i) + 1)

    def skip_ws(self) -> None:
        while self.i < self.n and self.s[self.i] in _WS:
            self.i += 1

    def end_of_line(self) -> None:
        """Only white space and a comment may follow on this line."""
        self.skip_ws()
        if self.peek() == "#":
            if self.i > 0 and self.s[self.i - 1] not in " \t\n":
                raise ReadError("a comment needs white space before it")
            while self.i < self.n and self.s[self.i] != "\n":
                self.i += 1
        if self.i < self.n and self.s[self.i] != "\n":
            raise ReadError(f"unexpected text {self.s[self.i:self.i + 20]!r} at line {self.line()}")

    def line(self) -> int:
        return self.s.count("\n", 0, self.i) + 1

    def marker_at(self, i: int) -> str | None:
        """'---' or '...' when a document marker starts at column 0 of position i."""
        if i == 0 or self.s[i - 1] == "\n":
            m = self.s[i:i + 3]
            if m in ("---", "...") and _is_ws_or_end(self.s[i + 3:i + 4]):
                return m
        return None

    def next_line(self, at_line_start: bool = False) -> int | None:
        """Move to the first content character of the next line that has content. Returns its
        column, or None at the end of the text or at a document marker (position left there)."""
        first = at_line_start
        while True:
            if not first:
                while self.i < self.n and self.s[self.i] != "\n":
                    self.i += 1
                if self.i >= self.n:
                    return None
                self.i += 1
            first = False
            k = self.i
            while k < self.n and self.s[k] == " ":
                k += 1
            spaces = k - self.i
            j = k
            while j < self.n and self.s[j] in "\t":
                j += 1
            if j >= self.n:
                self.i = self.n
                return None
            if self.s[j] == "\n":
                self.i = j
                continue
            if self.s[j] == "#":
                self.i = j
                continue
            if j > k:
                raise ReadError(f"a tab is used as indentation at line {self.s.count(chr(10), 0, k) + 1}")
            if spaces == 0 and self.marker_at(self.i):
                return None
            self.i = k
            return spaces

    # -- document
    def parse(self) -> Scalar | Seq | Map:
        if self.s.startswith("\ufeff"):
            self.i = 1
        col = self.next_line(at_line_start=True)
        while col is not None and self.peek() == "%":
            line = self.s[self.i:self.s.find("\n", self.i) if "\n" in self.s[self.i:] else self.n]
            if line.split("#")[0].split() not in (["%YAML", "1.1"], ["%YAML", "1.2"]):
                raise ReadError(f"directive {line.strip()!r} is outside what the kit reads")
            col = self.next_line()
        explicit = False
        if self.i < self.n and self.marker_at(self.i) == "---":
            explicit = True
            self.i += 3
            root = self.block_node(-1, inline_coll=False, seq_same=False)
        elif col is None:
            if not self.allow_empty:
                raise ReadError("no document: an empty stream is not a value")
            root = Scalar("", implicit_empty=True)
        else:
            root = self.block_node(-1, inline_coll=True, seq_same=False)
        if explicit or col is not None:
            self.end_of_line()
            col = self.next_line()
        if col is not None:
            raise ReadError(f"unexpected content after the document at line {self.line()}")
        if self.i < self.n:
            m = self.marker_at(self.i)
            if m == "...":
                self.i += 3
                self.end_of_line()
                if self.next_line() is not None or (self.i < self.n and self.marker_at(self.i)):
                    raise ReadError("more than one document")
            else:
                raise ReadError("more than one document")
        self.check_byte_order_marks()
        return root

    def check_byte_order_marks(self) -> None:
        """A byte order mark is not a character of plain or block scalars or comments (it is
        not an nb-char); only a quoted scalar may hold one. A document prefix may hold one too,
        and those were taken out before parsing."""
        pos = self.s.find("\ufeff")
        while pos >= 0:
            if not any(a <= pos < b for a, b in self.quoted_spans):
                raise ReadError("a byte order mark is written raw outside a quoted scalar; it must be escaped")
            pos = self.s.find("\ufeff", pos + 1)

    # -- properties
    def props(self) -> str | None:
        tag = None
        while True:
            c = self.peek()
            if c == "&":
                raise ReadError("an anchor is outside what the kit reads: a written value has no shared nodes")
            if c == "*":
                raise ReadError("an alias is outside what the kit reads: a written value has no shared nodes")
            if c != "!":
                return tag
            if tag is not None:
                raise ReadError("two tags on one node")
            j = self.i + 1
            if self.peek(1) == "<":
                k = self.s.find(">", j)
                if k < 0:
                    raise ReadError("unterminated verbatim tag")
                raw = self.s[j + 1:k]
                self.i = k + 1
                if not raw.startswith("tag:yaml.org,2002:"):
                    raise ReadError(f"tag !<{raw}> is outside what the kit reads")
                name = raw[len("tag:yaml.org,2002:"):]
            else:
                k = j
                while k < self.n and self.s[k] not in " \t\n" and self.s[k] not in _FLOW:
                    k += 1
                raw = self.s[self.i:k]
                self.i = k
                if raw == "!":
                    name = "!"
                elif raw.startswith("!!"):
                    name = raw[2:]
                else:
                    raise ReadError(f"tag {raw} is outside what the kit reads")
            if name != "!" and name not in _KNOWN_TAGS:
                raise ReadError(f"tag {raw} is outside what the kit reads")
            tag = name
            self.skip_ws()

    # -- block structure
    def enter(self):
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ReadError("nested more deeply than the kit reads")

    def block_node(self, parent: int, inline_coll: bool, seq_same: bool):
        """A node after an indicator (or at the start of the text). `parent` is the indent of
        the collection that holds it; `inline_coll` says whether a block collection may start
        on this line; `seq_same` lets a sequence sit at the parent's own indent (a mapping value)."""
        self.enter()
        try:
            self.skip_ws()
            col0 = self.col()
            self.prop_start = self.i
            tag = self.props()
            same_line = tag is not None
            c = self.peek()
            if c == "" or c == "\n" or (c == "#" and self.i > 0 and self.s[self.i - 1] in " \t\n"):
                save = self.i
                self.end_of_line()
                col = self.next_line()
                if col is None:
                    self.i = save
                    return Scalar("", tag=tag, implicit_empty=True)
                if col > parent:
                    if self.peek() in "!&*":
                        if tag is not None and self.peek() == "!":
                            raise ReadError("two tags on one node")
                        if tag is None:
                            return self.block_node(parent, True, False)
                        self.props()  # an anchor or alias: refused there
                    return self.content(parent, tag, True, False, col)
                if seq_same and col == parent and self.peek() == "-" and _is_ws_or_end(self.peek(1)):
                    return self.block_seq(col, tag)
                self.i = save
                return Scalar("", tag=tag, implicit_empty=True)
            return self.content(parent, tag, inline_coll, same_line, col0)
        finally:
            self.depth -= 1

    def content(self, parent, tag, coll_ok, tag_same_line, col0):
        c = self.peek()
        col = self.col()
        pstart = self.prop_start
        if c in "-?:" and _is_ws_or_end(self.peek(1)):
            if not coll_ok:
                raise ReadError(f"a block collection cannot start here (line {self.line()})")
            if c == "-":
                return self.block_seq(col, tag)
            return self.block_map(col, tag, None)
        if c in "|>":
            return self.block_scalar(parent, tag)
        start = self.i
        node, json_like = self.inline(parent, in_flow=False)
        end = self.i
        self.skip_ws()
        if self.peek() == ":" and _is_ws_or_end(self.peek(1)):
            # The limit counts the key's properties and the white space before the colon.
            self.check_key(pstart if tag_same_line else start, self.i)
            if not coll_ok:
                raise ReadError(f"a block mapping cannot start here (line {self.line()})")
            if tag_same_line:
                node.tag = tag
                tag = None
            return self.block_map(col0, tag, node)
        self.i = end
        if tag is not None:
            node.tag = tag
        return node

    def check_key(self, start: int, end: int) -> None:
        text = self.s[start:end]
        if "\n" in text:
            raise ReadError("an implicit key must be on one line")
        if len(text) > MAX_IMPLICIT_KEY:
            raise ReadError(f"an implicit key of {len(text)} characters is longer than {MAX_IMPLICIT_KEY}; "
                            "it needs the explicit `? ` form")

    def block_seq(self, col: int, tag):
        items = []
        while True:
            self.i += 1  # the '-'
            items.append(self.block_node(col, inline_coll=True, seq_same=False))
            save = self.i
            self.end_of_line()
            c = self.next_line()
            if c is None or c < col:
                self.i = save
                break
            if c > col:
                raise ReadError(f"wrong indentation at line {self.line()}")
            if not (self.peek() == "-" and _is_ws_or_end(self.peek(1))):
                self.i = save
                break
        return Seq(items, tag)

    def block_map(self, col: int, tag, first):
        pairs = []
        while True:
            if first is not None:
                key, first = first, None
                self.skip_ws()
            elif self.peek() == "?" and _is_ws_or_end(self.peek(1)):
                self.i += 1
                key = self.block_node(col, inline_coll=True, seq_same=True)
                save = self.i
                self.end_of_line()
                c = self.next_line()
                if c == col and self.peek() == ":" and _is_ws_or_end(self.peek(1)):
                    self.i += 1
                    val = self.block_node(col, inline_coll=True, seq_same=True)
                else:
                    self.i = save
                    val = Scalar("", implicit_empty=True)
                pairs.append((key, val))
                key = None
            elif self.peek() == ":" and _is_ws_or_end(self.peek(1)):
                key = Scalar("", implicit_empty=True)
            else:
                key = self.implicit_key(col)
            if key is not None:
                if not (self.peek() == ":" and _is_ws_or_end(self.peek(1))):
                    raise ReadError(f"expected ':' after a mapping key at line {self.line()}")
                self.i += 1
                pairs.append((key, self.block_node(col, inline_coll=False, seq_same=True)))
            save = self.i
            self.end_of_line()
            c = self.next_line()
            if c is None or c < col:
                self.i = save
                break
            if c > col:
                raise ReadError(f"wrong indentation at line {self.line()}")
        return Map(pairs, tag)

    def implicit_key(self, col: int):
        start = self.i
        tag = self.props()
        c = self.peek()
        if c == "" or c == "\n" or (c in "-?:" and _is_ws_or_end(self.peek(1))):
            raise ReadError(f"expected a mapping key at line {self.line()}")
        node, _ = self.inline(col, in_flow=False, single_line=True)
        self.skip_ws()
        self.check_key(start, self.i)
        if tag is not None:
            node.tag = tag
        return node

    # -- block scalars
    def block_scalar(self, parent: int, tag):
        style = self.peek()
        self.i += 1
        chomp, explicit = "clip", None
        for _ in range(2):
            c = self.peek()
            if c in "+-":
                chomp = "keep" if c == "+" else "strip"
                self.i += 1
            elif c in "123456789":
                explicit = int(c)
                self.i += 1
        self.end_of_line()
        raw: list[str] = []
        j = self.i
        while j + 1 < self.n:
            k = self.s.find("\n", j + 1)
            k = self.n if k < 0 else k
            raw.append(self.s[j + 1:k])
            j = k
        ind = None if explicit is None else max(parent, -1) + explicit
        if ind is not None and ind < 0:
            ind = 0
        lines: list[str] = []
        used = 0
        idx = 0
        if ind is None:
            while idx < len(raw) and raw[idx].strip(" ") == "":
                idx += 1
            if idx < len(raw):
                lead = len(raw[idx]) - len(raw[idx].lstrip(" "))
                if lead > parent and not (lead == 0 and self.line_is_marker(raw[idx])):
                    ind = lead
                    if any(len(blank) > lead for blank in raw[:idx]):
                        raise ReadError("a leading empty line of a block scalar has more spaces than its first "
                                        "text line; the scalar needs an indentation indicator")
            if ind is None:
                # No text: the blank lines are all there is, and `+` keeps them.
                lines = [""] * idx
                used = idx
        if ind is not None:
            for idx2, line in enumerate(raw):
                blank = line.strip(" ") == ""
                spaces = len(line) - len(line.lstrip(" "))
                if blank:
                    lines.append(line[ind:] if spaces > ind else "")
                elif spaces >= ind and not (ind == 0 and self.line_is_marker(line)):
                    lines.append(line[ind:])
                else:
                    break
                used = idx2 + 1
        # position: end of the last consumed line
        j = self.i
        for _ in range(used):
            k = self.s.find("\n", j + 1)
            j = self.n if k < 0 else k
        self.i = j
        lines = lines[:used]
        trailing = 0
        while lines and lines[-1] == "":
            lines.pop()
            trailing += 1
        if style == "|":
            body = "\n".join(lines)
        else:
            body = self.fold_block(lines)
        if lines:
            if chomp == "strip":
                text = body
            elif chomp == "clip":
                text = body + "\n"
            else:
                text = body + "\n" + "\n" * trailing
        else:
            text = "\n" * trailing if chomp == "keep" else ""
        return Scalar(text, style="literal" if style == "|" else "folded", tag=tag)

    def line_is_marker(self, line: str) -> bool:
        return line[:3] in ("---", "...") and _is_ws_or_end(line[3:4])

    @staticmethod
    def fold_block(lines: list[str]) -> str:
        if not lines:
            return ""
        lead = 0
        while lead < len(lines) and lines[lead] == "":
            lead += 1
        res = "\n" * lead
        if lead == len(lines):
            return res
        prev = lines[lead]
        res += prev
        k = 0
        for line in lines[lead + 1:]:
            if line == "":
                k += 1
                continue
            more = prev[:1] in (" ", "\t") or line[:1] in (" ", "\t")
            if more:
                res += "\n" * (k + 1)
            elif k == 0:
                res += " "
            else:
                res += "\n" * k
            res += line
            prev, k = line, 0
        return res

    # -- inline nodes (flow collections, quoted and plain scalars)
    def inline(self, parent: int, in_flow: bool, single_line: bool = False):
        c = self.peek()
        if c in "[{":
            return self.flow_collection(parent), True
        if c in "\"'":
            return self.quoted(parent), True
        if c in "*&":
            raise ReadError("an anchor or alias is outside what the kit reads")
        return self.plain(parent, in_flow, single_line), False

    def plain(self, parent: int, in_flow: bool, single_line: bool = False) -> Scalar:
        c = self.peek()
        nxt = self.peek(1)
        if c == "" or c == "\n" or c in ",[]{}#&*!|>'\"%@`":
            raise ReadError(f"{c!r} cannot start a plain scalar (line {self.line()})")
        if c in "-?:" and (_is_ws_or_end(nxt) or (in_flow and nxt in _FLOW)):
            raise ReadError(f"{c!r} cannot start a plain scalar (line {self.line()})")
        lines: list[str] = []
        while True:
            seg_start = self.i
            while self.i < self.n and self.s[self.i] != "\n":
                ch = self.s[self.i]
                nx = self.s[self.i + 1:self.i + 2]
                if ch == ":" and (_is_ws_or_end(nx) or (in_flow and nx in _FLOW and nx != "")):
                    break
                if ch == "#" and self.i > seg_start and self.s[self.i - 1] in _WS:
                    break
                if in_flow and ch in _FLOW:
                    break
                self.i += 1
            seg = self.s[seg_start:self.i].rstrip(" \t")
            lines.append(seg)
            if self.i < self.n and self.s[self.i] != "\n":
                break  # stopped at an indicator
            if single_line or self.i >= self.n:
                break
            cont = self.continuation(parent)
            if cont is None:
                break
            blanks, self.i = cont
            lines.extend([""] * blanks)
        return Scalar(self.fold_plain(lines))

    @staticmethod
    def fold_plain(lines: list[str]) -> str:
        res, k = lines[0], 0
        for line in lines[1:]:
            if line == "":
                k += 1
                continue
            res += (" " if k == 0 else "\n" * k) + line
            k = 0
        return res

    def continuation(self, parent: int):
        """Whether the scalar goes on after the line break at self.i. Returns (blank lines,
        position of the first character of the next line) or None."""
        j = self.i
        blanks = 0
        while True:
            k = j + 1
            if k > self.n:
                return None
            eol = self.s.find("\n", k)
            eol = self.n if eol < 0 else eol
            line = self.s[k:eol]
            if line.strip(" \t") == "":
                if eol >= self.n:
                    return None
                blanks += 1
                j = eol
                continue
            spaces = len(line) - len(line.lstrip(" "))
            text = line.lstrip(" \t")
            if text.startswith("#") or (text.startswith(":") and _is_ws_or_end(text[1:2])):
                return None
            if spaces < parent + 1:
                return None
            if spaces == 0 and self.marker_at(k):
                return None
            return blanks, k + (len(line) - len(line.lstrip(" \t")))

    def quoted(self, parent: int) -> Scalar:
        q = self.peek()
        start_line = self.line()
        span_start = self.i
        self.i += 1
        out: list[str] = []
        ws_start = None
        while True:
            if self.i >= self.n:
                raise ReadError(f"unterminated quoted scalar from line {start_line}")
            ch = self.s[self.i]
            if ch == q:
                if q == "'" and self.peek(1) == "'":
                    out.append("'")
                    ws_start = None
                    self.i += 2
                    continue
                self.i += 1
                break
            if ch == "\\" and q == '"':
                e = self.peek(1)
                if e == "\n":
                    self.i += 2
                    breaks = self.skip_to_text(parent)
                    out.append("\n" * breaks)
                    ws_start = None
                    continue
                if e in _ESCAPES:
                    out.append(_ESCAPES[e])
                    self.i += 2
                elif e in _HEX_ESCAPES:
                    w = _HEX_ESCAPES[e]
                    digits = self.s[self.i + 2:self.i + 2 + w]
                    if len(digits) != w or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                        raise ReadError(f"bad \\{e} escape at line {self.line()}")
                    cp = int(digits, 16)
                    if cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
                        raise ReadError(f"\\{e}{digits} is not a character")
                    out.append(chr(cp))
                    self.i += 2 + w
                else:
                    raise ReadError(f"unknown escape \\{e} at line {self.line()}")
                ws_start = None
                continue
            if ch == "\n":
                if ws_start is not None:
                    del out[ws_start:]
                    ws_start = None
                self.i += 1
                breaks = 1 + self.skip_to_text(parent)
                out.append(" " if breaks == 1 else "\n" * (breaks - 1))
                continue
            if ch in _WS:
                if ws_start is None:
                    ws_start = len(out)
            else:
                ws_start = None
            out.append(ch)
            self.i += 1
        self.quoted_spans.append((span_start, self.i))
        return Scalar("".join(out), style="single" if q == "'" else "double")

    def skip_to_text(self, parent: int) -> int:
        """At the start of a line inside a quoted scalar (the line break is consumed): pass blank
        lines and the indentation of the next text line. Returns the number of blank lines."""
        blanks = 0
        while True:
            k = self.i
            while k < self.n and self.s[k] == " ":
                k += 1
            spaces = k - self.i
            j = k
            while j < self.n and self.s[j] in _WS:
                j += 1
            if j < self.n and self.s[j] == "\n":
                blanks += 1
                self.i = j + 1
                continue
            if j >= self.n:
                raise ReadError("unterminated quoted scalar")
            if spaces == 0 and self.marker_at(self.i):
                raise ReadError("a document marker inside a quoted scalar")
            if spaces < parent + 1:
                raise ReadError(f"a continuation line of a quoted scalar is not indented (line {self.line()})")
            self.i = j
            return blanks

    # -- flow collections
    def ws_nl(self, parent: int) -> None:
        """White space, line breaks and comments between flow tokens."""
        while self.i < self.n:
            ch = self.s[self.i]
            if ch in _WS:
                self.i += 1
            elif ch == "\n":
                self.i += 1
                k = self.i
                while k < self.n and self.s[k] == " ":
                    k += 1
                if k < self.n and self.s[k] not in "\n" and (k - self.i) < parent + 1 and self.s[k] != "#":
                    raise ReadError(f"a line inside a flow collection is not indented (line {self.line()})")
                if self.marker_at(self.i):
                    raise ReadError("a document marker inside a flow collection")
            elif ch == "#" and self.i > 0 and self.s[self.i - 1] in " \t\n":
                while self.i < self.n and self.s[self.i] != "\n":
                    self.i += 1
            else:
                return

    def flow_collection(self, parent: int):
        self.enter()
        try:
            is_map = self.peek() == "{"
            close = "}" if is_map else "]"
            self.i += 1
            items = []
            while True:
                self.ws_nl(parent)
                if self.peek() == close:
                    self.i += 1
                    break
                if self.peek() == "":
                    raise ReadError("unterminated flow collection")
                items.append(self.flow_entry(parent, is_map))
                self.ws_nl(parent)
                if self.peek() == ",":
                    self.i += 1
                    continue
                if self.peek() == close:
                    self.i += 1
                    break
                raise ReadError(f"expected ',' or '{close}' at line {self.line()}")
            return Map(items) if is_map else Seq(items)
        finally:
            self.depth -= 1

    def flow_node(self, parent: int):
        tag = self.props()
        if tag is not None and self.peek() in " \t\n#":
            self.ws_nl(parent)
        c = self.peek()
        if c == "" or c in ",]}":
            return Scalar("", tag=tag, implicit_empty=True), False
        if c == ":" and (_is_ws_or_end(self.peek(1)) or self.peek(1) in _FLOW):
            return Scalar("", tag=tag, implicit_empty=True), False
        node, json_like = self.inline(parent, in_flow=True)
        if tag is not None:
            node.tag = tag
        return node, json_like

    def flow_entry(self, parent: int, is_map: bool):
        explicit = False
        if self.peek() == "?" and (_is_ws_or_end(self.peek(1)) or self.peek(1) in _FLOW):
            explicit = True
            self.i += 1
            self.ws_nl(parent)
        start = self.i
        key, json_like = self.flow_node(parent)
        end = self.i
        if is_map:
            # A flow mapping key may span lines; the colon may be on a later line.
            self.ws_nl(parent)
        else:
            self.skip_ws()
        nx = self.peek(1)
        if self.peek() == ":" and (json_like or _is_ws_or_end(nx) or nx in _FLOW):
            if not is_map and not explicit:
                # A pair inside a sequence is a single-line implicit key.
                self.check_key(start, self.i)
            self.i += 1
            self.ws_nl(parent)
            if self.peek() in ("", ",", "]", "}"):
                val = Scalar("", implicit_empty=True)
            else:
                val, _ = self.flow_node(parent)
            pair = (key, val)
            return pair if is_map else Map([pair])
        if is_map or explicit:
            if getattr(key, "implicit_empty", False) and not explicit and is_map:
                raise ReadError(f"an empty entry in a flow mapping at line {self.line()}")
            pair = (key, Scalar("", implicit_empty=True))
            return pair if is_map else Map([pair])
        if getattr(key, "implicit_empty", False):
            raise ReadError(f"an empty entry in a flow sequence at line {self.line()}")
        return key


# --------------------------------------------------------------------------
# Resolution


def _inf(sign: str) -> dict:
    return {"$float": "-inf" if sign == "-" else "inf"}


_NAN = {"$float": "nan"}

_CORE_INT10 = re.compile(r"[-+]?[0-9]+")
_CORE_INT8 = re.compile(r"0o[0-7]+")
_CORE_INT16 = re.compile(r"0x[0-9a-fA-F]+")
_CORE_FLOAT = re.compile(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?")
_CORE_INF = re.compile(r"([-+]?)\.(inf|Inf|INF)")
_CORE_NAN = re.compile(r"\.(nan|NaN|NAN)")

_Y11_BOOL = {"y": True, "Y": True, "yes": True, "Yes": True, "YES": True, "n": False, "N": False,
             "no": False, "No": False, "NO": False, "true": True, "True": True, "TRUE": True,
             "false": False, "False": False, "FALSE": False, "on": True, "On": True, "ON": True,
             "off": False, "Off": False, "OFF": False}
_Y11_INT_BIN = re.compile(r"([-+]?)0b([0-1_]+)")
_Y11_INT_OCT = re.compile(r"([-+]?)0([0-7_]+)")
_Y11_INT_DEC = re.compile(r"([-+]?)(0|[1-9][0-9_]*)")
_Y11_INT_HEX = re.compile(r"([-+]?)0x([0-9a-fA-F_]+)")
_Y11_INT_SEX = re.compile(r"([-+]?)([1-9][0-9_]*(?::[0-5]?[0-9])+)")
_Y11_FLOAT = re.compile(r"[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?"
                        r"|[-+]?\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?")
_Y11_FLOAT_SEX = re.compile(r"([-+]?)([0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*)")
_Y11_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}"
                            r"|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{1,2}:[0-9]{1,2}"
                            r"(?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?")

# Number forms that readers accept beyond both schemas.
_RADIX = {"x": "0-9a-fA-F_", "o": "0-7_", "b": "01_"}
_QUIRK_RADIX = re.compile(r"[-+]?0([xXoObB])[-+]?([0-9a-fA-F_]+)")
_QUIRK_SIGNS = re.compile(r"[-+]{2,}(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?")


_MARKERS = {
    "timestamp": "<a timestamp in YAML 1.1>",
    "merge": "<the merge key in YAML 1.1>",
    "value": "<the value key in YAML 1.1>",
    "number": "<a number to common readers>",
}


def _marker(kind: str) -> str:
    """What stands for a plain scalar that a reader takes as something other than a string. It
    is text that names the reading, so a failure says what was read. It never equals a case's
    value: the kit's tests check that no case holds one of these texts."""
    return _MARKERS[kind]


def _sexagesimal(body: str, base_float: bool):
    parts = body.replace("_", "").split(":")
    total = 0
    for p in parts[:-1]:
        total = total * 60 + int(p)
    if base_float:
        return total * 60 + float(parts[-1] if parts[-1] else "0")
    return total * 60 + int(parts[-1])


class Schema:
    name = CORE

    def resolve(self, text: str):
        raise NotImplementedError

    def typed(self, tag: str, text: str):
        v = self.resolve(text)
        ok = {"int": lambda x: isinstance(x, int) and not isinstance(x, bool),
              "float": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool) or
              (isinstance(x, dict) and set(x) == {"$float"}),
              "bool": lambda x: isinstance(x, bool),
              "null": lambda x: x is None}[tag]
        if not ok(v):
            raise ReadError(f"!!{tag} {text!r} does not read as that type")
        if tag == "float" and isinstance(v, int):
            return float(v)
        return v


class CoreSchema(Schema):
    name = CORE

    def resolve(self, text: str):
        if text in ("", "~", "null", "Null", "NULL"):
            return None
        if text in ("true", "True", "TRUE"):
            return True
        if text in ("false", "False", "FALSE"):
            return False
        if _CORE_INT10.fullmatch(text):
            return int(text)
        if _CORE_INT8.fullmatch(text):
            return int(text[2:], 8)
        if _CORE_INT16.fullmatch(text):
            return int(text[2:], 16)
        m = _CORE_INF.fullmatch(text)
        if m:
            return _inf(m.group(1))
        if _CORE_NAN.fullmatch(text):
            return dict(_NAN)
        if _CORE_FLOAT.fullmatch(text):
            return float(text)
        return text


class Yaml11Schema(Schema):
    name = YAML11

    def resolve(self, text: str):
        if text in ("", "~", "null", "Null", "NULL"):
            return None
        if text in _Y11_BOOL:
            return _Y11_BOOL[text]
        for rx, base in ((_Y11_INT_BIN, 2), (_Y11_INT_HEX, 16)):
            m = rx.fullmatch(text)
            if m:
                return self._int(m, base)
        m = _Y11_INT_OCT.fullmatch(text)
        if m:
            return self._int(m, 8)
        m = _Y11_INT_DEC.fullmatch(text)
        if m:
            return self._int(m, 10)
        m = _Y11_INT_SEX.fullmatch(text)
        if m:
            v = _sexagesimal(m.group(2), False)
            return -v if m.group(1) == "-" else v
        m = _CORE_INF.fullmatch(text)
        if m:
            return _inf(m.group(1))
        if _CORE_NAN.fullmatch(text):
            return dict(_NAN)
        if _Y11_FLOAT.fullmatch(text):
            return float(text.replace("_", ""))
        m = _Y11_FLOAT_SEX.fullmatch(text)
        if m:
            v = _sexagesimal(m.group(2), True)
            return -v if m.group(1) == "-" else v
        if _Y11_TIMESTAMP.fullmatch(text):
            return _marker("timestamp")
        if text == "<<":
            return _marker("merge")
        if text == "=":
            return _marker("value")
        return text

    @staticmethod
    def _int(m, base: int):
        v = int(m.group(2).replace("_", "") or "0", base)
        return -v if m.group(1) == "-" else v


class CommonSchema(Yaml11Schema):
    """YAML 1.1 plus number forms that common readers take beyond either schema. A plain
    scalar that stays a string under 1.1 but has one of those forms is reported as a number."""

    name = COMMON

    def resolve(self, text: str):
        v = Yaml11Schema.resolve(self, text)
        if isinstance(v, str) and quirk_number(text):
            return _marker("number")
        return v


def quirk_number(text: str) -> bool:
    """Whether some widely used reader reads this plain text as a number although neither
    YAML 1.2 Core nor YAML 1.1 does."""
    m = _QUIRK_RADIX.fullmatch(text)
    if m and re.fullmatch("[" + _RADIX[m.group(1).lower()] + "]+", m.group(2)) and \
            any(ch != "_" for ch in m.group(2)):
        return True
    if _QUIRK_SIGNS.fullmatch(text):
        return True
    if "_" in text:
        bare = text.replace("_", "")
        if bare and (_CORE_INT10.fullmatch(bare) or _CORE_FLOAT.fullmatch(bare)
                     or _CORE_INT16.fullmatch(bare) or _CORE_INT8.fullmatch(bare)
                     or _Y11_INT_BIN.fullmatch(bare)):
            return True
        # An underscore inside a radix prefix: `0_x123`.
        if bare != text and (_QUIRK_RADIX.fullmatch(bare)):
            return True
    return False


SCHEMAS = {CORE: CoreSchema(), YAML11: Yaml11Schema(), COMMON: CommonSchema()}


def to_value(node, schema: Schema, depth: int = 0):
    if depth > MAX_DEPTH:
        raise ReadError("nested more deeply than the kit reads")
    if isinstance(node, Scalar):
        t = node.tag
        if t in ("map", "seq"):
            raise ReadError(f"!!{t} on a scalar")
        if t in ("str", "!"):
            return node.text
        try:
            if t is None:
                if node.style != "plain":
                    return node.text
                return schema.resolve(node.text)
            return schema.typed(t, node.text)
        except ValueError as e:  # an integer with more digits than the reader converts
            raise ReadError(f"the number {node.text[:20]!r}... has {len(node.text)} characters, "
                            "more than the reader converts") from e
    if isinstance(node, Seq):
        if node.tag not in (None, "seq", "!"):
            raise ReadError(f"!!{node.tag} on a sequence")
        return [to_value(x, schema, depth + 1) for x in node.items]
    if node.tag not in (None, "map", "!"):
        raise ReadError(f"!!{node.tag} on a mapping")
    pairs = [(to_value(k, schema, depth + 1), to_value(v, schema, depth + 1)) for k, v in node.pairs]
    seen = set()
    for k, _ in pairs:
        try:
            d = kitjson.digests(k)[id(k)]
        except ValueError as e:
            raise ReadError(str(e)) from e
        if d in seen:
            raise ReadError(f"two keys of one mapping read as the same key, {kitjson.short(k, 40)}")
        seen.add(d)
    if all(isinstance(k, str) and not k.startswith("$") for k, _ in pairs):
        return {k: v for k, v in pairs}
    return {"$map": [[k, v] for k, v in pairs]}


# --------------------------------------------------------------------------
# Judging

_RAW_LINE_BREAKS = {"\u0085": "NEL", "\u2028": "LS", "\u2029": "PS", "\ufeff": "BOM"}


def decode(out: bytes) -> str:
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ReadError(f"the output is not UTF-8: {e}") from e


def parse(text: str, allow_empty: bool = True):
    return _parse(text, allow_empty)[0]


def _parse(text: str, allow_empty: bool):
    """The tree and the text as it was read (line breaks normalised, prefix marks removed)."""
    try:
        parser = Parser(text, allow_empty)
        return parser.parse(), parser.s
    except RecursionError as e:
        raise ReadError("nested more deeply than the kit reads") from e


def judge(out: bytes, value) -> dict:
    """{"core": problem or "", "convention": problem or ""} for written bytes and the value
    they were written for. `core` is the Core-schema read-back; `convention` adds YAML 1.1, the
    number forms common readers accept, and characters 1.1 reads as line breaks or a BOM."""
    try:
        text = decode(out)
        root, read = _parse(text, allow_empty=False)
    except ReadError as e:
        msg = f"the output does not read as YAML: {e}"
        return {"core": msg, "convention": msg}
    res = {"core": _read_diff(root, SCHEMAS[CORE], value), "convention": ""}
    raw = sorted({_RAW_LINE_BREAKS[c] for c in read if c in _RAW_LINE_BREAKS})
    if raw:
        res["convention"] = ("written raw: " + ", ".join(raw) +
                             " (YAML 1.1 reads NEL, LS and PS as line breaks and a BOM is not text)")
    else:
        d = _read_diff(root, SCHEMAS[COMMON], value)
        if d:
            res["convention"] = "under YAML 1.1 and common readers: " + d
    return res


def _read_diff(root, schema: Schema, value) -> str:
    try:
        got = to_value(root, schema)
    except ReadError as e:
        return f"reading as {schema.name}: {e}"
    return kitjson.value_diff(value, got)
