"""Independent llama.cpp GBNF consumer used to validate `strata grammar`.

This module is deliberately written as a *real consumer* of the GBNF dialect
llama.cpp's grammar engine understands. It does NOT reuse the grammar-as-code
tables in strata/grammar.py: it parses the emitted GBNF text with its own
tokenizer/parser and then asks the resulting grammar whether a source program
is accepted, using the same character-level matching llama.cpp performs against
the text of each token.

Two entry points:

- ``GbnfGrammar.from_text(text)`` — parse a GBNF document into a grammar.
- ``GbnfGrammar.accepts(text)`` — does the fully-matched string belong to the
  language (character-level, whitespace must be handled by the caller)?
- ``strata.gbnf.accepts_program(lexer_tokens)`` — convenience wrapper: rebuilds
  the token spelling stream (STR tokens are re-serialized with quotes and
  escapes) and accepts/closes over the whole stream. Whitespace is stripped by
  the Strata lexer, mirroring the whitespace-agnostic grammar.

Known inherent limitations of character-level matching (identical to what
llama.cpp itself sees):

- Keywords are not token-kind aware: ``ident`` also matches the spelling of
  reserved words, so a char-level ``ident`` terminal accepts spellings the
  Strata parser (token-based) rejects. This is unavoidable in GBNF and is why
  identifiers that collide with keywords are surfaced as accepted-but-rejected.
- ``take N .. M``: the parser requires the range dots as two ``.`` tokens, but
  the lexer and llama.cpp both absorb a trailing ``.digits`` into a single
  FLOAT token (``.10``), so the ``N..M`` spelling is only producible as
  ``N .. M`` (with spaces). ``tests/test_gbnf_consumer.py`` pins the set of
  sampled spellings that are accepted char-level but rejected by the parser to
  exactly these two classes.

Supported dialect (per llama.cpp gbnf-parser.cpp):

- ``name ::= alt | alt`` rules, ``#`` line comments
- rule references (bare names)
- ``"quoted literals"`` with ``\\n \\t \\" \\\\ \\]`` escapes
- char classes ``[...]`` with ranges, escapes and ``^`` negation
- grouping ``(...)``, alternation ``|``
- repetition ``*``, ``+``, ``?`` (and ``{n}``/``{n,m}`` quantifiers)
- ``.`` matches any single character

Matching is set-based (returns every reachable position) instead of
PEG-greedy, so any accepting derivation is found and ambiguity is harmless.
Grammar cycles that consume no input are cut off (an accepting derivation can
never depend on a same-position cycle).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple, Union


class GbnfError(Exception):
    """Raised for malformed GBNF input."""


# ---------------------------------------------------------------------------
# AST for a parsed GBNF rule body
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lit:
    text: str


@dataclass(frozen=True)
class CharClass:
    allow: frozenset  # allowed characters
    negate: bool = False


@dataclass(frozen=True)
class AnyChar:
    pass


@dataclass(frozen=True)
class Ref:
    name: str


@dataclass(frozen=True)
class Seq:
    items: tuple


@dataclass(frozen=True)
class Alt:
    branches: tuple


@dataclass(frozen=True)
class Rep:
    inner: object
    lo: int
    hi: Optional[int]  # None == unbounded


Node = Union[Lit, CharClass, AnyChar, Ref, Seq, Alt, Rep]


# ---------------------------------------------------------------------------
# GBNF text tokenizer
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _gbnf_tokens(text: str) -> List[Tuple[str, str]]:
    toks: List[Tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text.startswith("::=", i):
            toks.append(("SEP", ""))
            i += 3
            continue
        if c == "|":
            toks.append(("PIPE", ""))
            i += 1
            continue
        if c == "(":
            toks.append(("LPAREN", ""))
            i += 1
            continue
        if c == ")":
            toks.append(("RPAREN", ""))
            i += 1
            continue
        if c == "*":
            toks.append(("STAR", ""))
            i += 1
            continue
        if c == "+":
            toks.append(("PLUS", ""))
            i += 1
            continue
        if c == "?":
            toks.append(("QMARK", ""))
            i += 1
            continue
        if c == ".":
            toks.append(("ANY", ""))
            i += 1
            continue
        if c == "{":
            j = i + 1
            m = re.match(r"\s*(\d+)\s*,?\s*(\d+)?\s*\}", text[j:])
            if m:
                lo, hi = int(m.group(1)), m.group(2)
                toks.append(("BRAKE", f"{lo},{hi}" if hi else str(lo)))
                i = j + m.end()
                continue
            raise GbnfError(f"invalid repetition quantifier at column {i}")
        if c == "[":
            j, buf = i + 1, []
            while j < n and text[j] != "]":
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j:j + 2])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            if j >= n:
                raise GbnfError(f"unterminated char class at column {i}")
            toks.append(("CLASS", "".join(buf)))
            i = j + 1
            continue
        if c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j:j + 2])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            if j >= n:
                raise GbnfError(f"unterminated literal at column {i}")
            toks.append(("LIT", "".join(buf)))
            i = j + 1
            continue
        m = _TOKEN_RE.match(text, i)
        if m:
            toks.append(("NAME", m.group(0)))
            i = m.end()
            continue
        raise GbnfError(f"unexpected character {c!r} at column {i}")
    return toks


# ---------------------------------------------------------------------------
# char class handling
# ---------------------------------------------------------------------------


_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\",
    "]": "]", "-": "-", "^": "^", "[": "[", " ": " ", "x": "x",
}


def _unescape(raw: str) -> str:
    out: List[str] = []
    i, n = 0, len(raw)
    while i < n:
        if raw[i] == "\\" and i + 1 < n:
            out.append(_ESCAPES.get(raw[i + 1], raw[i + 1]))
            i += 2
        else:
            out.append(raw[i])
            i += 1
    return "".join(out)


def _expand_class(raw: str) -> Tuple[FrozenSet[str], bool]:
    negate = raw.startswith("^")
    body = raw[1:] if negate else raw
    chars: Set[str] = set()
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "\\" and i + 1 < n:
            chars.add(_unescape(body[i:i + 2]))
            i += 2
            continue
        if ch == "-" and i + 1 < n:
            # dash in a middle position: treat as literal unless a range is
            # impossible (start can't be a range opener at i==0 here).
            if i > 0 and body[i - 1] != "\\":
                chars.add("-")
            else:
                chars.add("-")
            i += 1
            continue
        if i + 2 < n and body[i + 1] == "-" and body[i + 2] != "-":
            a = ch
            b = _unescape(body[i + 2:i + 3]) if body[i + 2] == "\\" and i + 3 < n else body[i + 2]
            try:
                for code in range(ord(a), ord(b) + 1):
                    chars.add(chr(code))
            except (TypeError, ValueError):
                chars.add(a)
                chars.add("-")
                chars.add(b)
            i += 3
            continue
        chars.add(ch)
        i += 1
    return frozenset(chars), negate


# ---------------------------------------------------------------------------
# GBNF parser: token stream -> AST
# ---------------------------------------------------------------------------


class _Parser:
    def __init__(self, toks: Sequence[Tuple[str, str]]):
        self.toks = toks
        self.i = 0

    def peek(self) -> Optional[Tuple[str, str]]:
        """Next (kind, value) token without consuming, or None at end of input."""
        return self.toks[self.i] if self.i < len(self.toks) else None

    def next(self) -> Tuple[str, str]:
        """Consume and return the next (kind, value) token."""
        t = self.toks[self.i]
        self.i += 1
        return t

    def at(self, kind: str) -> bool:
        """True when the next unconsumed token has the given kind."""
        t = self.peek()
        return t is not None and t[0] == kind

    # element -> optional suffix (Rep/None)
    def _atom(self) -> Node:
        t = self.peek()
        if t is None:
            raise GbnfError("unexpected end of rule")
        if t[0] == "NAME":
            self.next()
            return Ref(t[1])
        if t[0] == "LIT":
            self.next()
            return Lit(_unescape(t[1] if len(t[1]) else ""))
        if t[0] == "CLASS":
            self.next()
            allow, neg = _expand_class(t[1])
            return CharClass(allow, neg)
        if t[0] == "ANY":
            self.next()
            return AnyChar()
        if t[0] == "LPAREN":
            self.next()
            body = self._expr()
            if not self.at("RPAREN"):
                raise GbnfError(f"expected ')' got {self.peek()}")
            self.next()
            return body
        raise GbnfError(f"unexpected {t[0]}")

    def _suffix(self, base: Node) -> Node:
        t = self.peek()
        if t is None or t[0] not in ("STAR", "PLUS", "QMARK", "BRAKE"):
            return base
        self.next()
        if t[0] == "STAR":
            return Rep(base, 0, None)
        if t[0] == "PLUS":
            return Rep(base, 1, None)
        if t[0] == "QMARK":
            return Rep(base, 0, 1)
        lo_s, _, hi_s = t[1].partition(",")
        lo = int(lo_s) if lo_s else 0
        hi = int(hi_s) if hi_s else None
        return Rep(base, lo, hi)

    def _seq(self) -> Node:
        items: List[Node] = []
        while True:
            t = self.peek()
            if t is None or t[0] in ("PIPE", "RPAREN"):
                break
            items.append(self._suffix(self._atom()))
        if not items:
            raise GbnfError("empty alternative")
        return items[0] if len(items) == 1 else Seq(tuple(items))

    def _expr(self) -> Node:
        branches = [self._seq()]
        while self.at("PIPE"):
            self.next()
            branches.append(self._seq())
        return branches[0] if len(branches) == 1 else Alt(tuple(branches))

    def parse_root(self) -> Node:
        """Parse the full token stream into a rule node, rejecting trailing tokens."""
        node = self._expr()
        if self.i != len(self.toks):
            raise GbnfError(f"trailing tokens: {self.toks[self.i:]}")
        return node


# ---------------------------------------------------------------------------
# grammar document
# ---------------------------------------------------------------------------


@dataclass
class GbnfGrammar:
    rules: Dict[str, Node] = field(default_factory=dict)

    @classmethod
    def from_text(cls, text: str) -> "GbnfGrammar":
        """Parse a GBNF document string into a GbnfGrammar."""
        g = cls()
        toks_all = _gbnf_tokens(text)
        n = len(toks_all)
        i = 0
        # rules (and their bodies) are separated by top-level `::=`. A `::=`
        # may appear inside a parenthesised/braced group? never -- the emit
        # format is one `name ::= body` per line, so every SEP splits rules.
        while i < n:
            t = toks_all[i]
            if t[0] == "SEP":
                raise GbnfError(f"unexpected '::=' at token {i}")
            if t[0] != "NAME":
                raise GbnfError(f"expected rule name got {t[0]!r} ({t[1]!r})")
            name = t[1]
            i += 1
            if toks_all[i][0] == "SEP":
                i += 1
            else:
                raise GbnfError(f"rule {name!r}: expected '::='")
            body_end = i
            depth = 0
            while body_end < n:
                tj = toks_all[body_end]
                if tj[0] == "RPAREN":
                    depth -= 1
                    if depth < 0:
                        raise GbnfError(f"unbalanced ')' in rule {name}")
                elif tj[0] == "LPAREN":
                    depth += 1
                elif depth == 0:
                    # a NAME directly followed by `::=` is the header of the
                    # next rule, never part of this body
                    nxt = toks_all[body_end + 1] if body_end + 1 < n else None
                    if tj[0] == "SEP" or nxt is not None and nxt[0] == "SEP":
                        break
                body_end += 1
            body_toks = toks_all[i:body_end]
            parser = _Parser(body_toks)
            body = parser.parse_root()
            if name in g.rules:
                raise GbnfError(f"duplicate rule {name!r}")
            g.rules[name] = body
            i = body_end  # body_end sits at the next rule's NAME (or EOF)
        if "root" not in g.rules:
            raise GbnfError("grammar has no 'root' rule")
        return g

    def validate(self) -> List[str]:
        """Every referenced rule is defined. Returns a list of problems."""
        names = set(self.rules)
        problems: List[str] = []
        for name, node in self.rules.items():
            for ref in collect_refs(node):
                if ref not in names and ref != name:
                    problems.append(f"rule {name!r} references undefined {ref!r}")
        return problems

    # -- matching ------------------------------------------------------------

    def _match(self, text: str, node: Node, pos: int,
               memo: Dict[Tuple, FrozenSet[int]],
               pending: Set[Tuple]) -> FrozenSet[int]:
        if isinstance(node, Lit):
            end = pos + len(node.text)
            return frozenset({end}) if text.startswith(node.text, pos) else frozenset()
        if isinstance(node, AnyChar):
            return frozenset({pos + 1}) if pos < len(text) else frozenset()
        if isinstance(node, CharClass):
            if pos >= len(text):
                return frozenset()
            ok = text[pos] in node.allow
            if node.negate:
                ok = not ok
            return frozenset({pos + 1}) if ok else frozenset()
        if isinstance(node, Ref):
            key = (node.name, pos)
            if key in memo:
                return memo[key]
            if key in pending:
                return frozenset()
            pending.add(key)
            res = self._match(text, self.rules[node.name], pos, memo, pending)
            pending.discard(key)
            memo[key] = res
            return res
        if isinstance(node, Seq):
            cur: FrozenSet[int] = frozenset({pos})
            for item in node.items:
                nxt: Set[int] = set()
                for p in cur:
                    nxt |= self._match(text, item, p, memo, pending)
                cur = frozenset(nxt)
                if not cur:
                    break
            return cur
        if isinstance(node, Alt):
            res: Set[int] = set()
            for br in node.branches:
                res |= self._match(text, br, pos, memo, pending)
            return frozenset(res)
        if isinstance(node, Rep):
            reach: Set[int] = {pos}
            frontier: Set[int] = {pos}
            steps = 0
            while frontier and steps < len(text) + 1:
                nxt: Set[int] = set()
                for p in frontier:
                    nxt |= self._match(text, node.inner, p, memo, pending)
                frontier = nxt - reach
                reach |= nxt
                steps += 1
            if node.hi is not None:
                # rebuild: positions reachable in exactly lo..hi iterations.
                level: Set[int] = {pos}
                out: Set[int] = set()
                if node.lo <= 0:
                    out |= {pos}
                for _ in range(1, node.hi + 1):
                    nxt = set()
                    for p in level:
                        nxt |= self._match(text, node.inner, p, memo, pending)
                    level = nxt
                    if _ >= node.lo:
                        out |= level
                return frozenset(out)
            return frozenset(reach)
        raise GbnfError(f"unknown node {node!r}")

    def accept_positions(self, text: str, start: str = "root") -> FrozenSet[int]:
        """All end positions reachable from rule 'start' matching `text`."""
        memo: Dict[Tuple, FrozenSet[int]] = {}
        return self._match(text, self.rules[start], 0, memo, set())

    def accepts(self, text: str) -> bool:
        """Whether the start rule fully consumes `text`."""
        return len(text) in self.accept_positions(text)


def collect_refs(node: Node) -> List[str]:
    """Recursively collect Ref leaf names from a GBNF node."""
    if isinstance(node, Ref):
        return [node.name]
    if isinstance(node, Seq):
        out: List[str] = []
        for it in node.items:
            out.extend(collect_refs(it))
        return out
    if isinstance(node, Alt):
        out = []
        for br in node.branches:
            out.extend(collect_refs(br))
        return out
    if isinstance(node, Rep):
        return collect_refs(node.inner)
    return []


# ---------------------------------------------------------------------------
# Strata integration: token spelling stream -> char stream for the engine
# ---------------------------------------------------------------------------


def token_spelling(tokens: Sequence[object]) -> str:
    """Rebuild the character stream llama.cpp would match against.

    Tokens come from strata.lexer; STR tokens are re-serialized to their
    quoted form (with the escapes the GBNF string terminal expects) because
    the lexer stores decoded parts. ID/KW/TYPE_KW/SYM/INT/FLOAT contribute
    their plain value. Whitespace and comments are already absent from the
    token stream (whitespace-agnostic grammar).
    """
    parts: List[str] = []
    for tok in tokens:
        if tok.kind == "EOF":
            continue
        if tok.kind == "STR":
            s = ['"']
            for kind, val in tok.value:
                if kind == "var":
                    s.append("${%s}" % val)
                else:
                    s.append(val.replace("\\", "\\\\").replace('"', '\\"')
                             .replace("\n", "\\n").replace("\t", "\\t"))
            s.append('"')
            parts.append("".join(s))
        else:
            parts.append(str(tok.value))
    return "".join(parts)


def accepts_program(tokens: Sequence[object], grammar: GbnfGrammar) -> bool:
    """Whether the token stream is accepted by the grammar (for completion filtering)."""
    return grammar.accepts(token_spelling(tokens))