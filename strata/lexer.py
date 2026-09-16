"""Strata lexer: tokenizer producing tokens with byte/line spans."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


class LexError(Exception):
    pass


@dataclass
class Token:
    kind: str
    value: object
    line: int
    col: int

    def __repr__(self):
        return f"Token({self.kind}, {self.value!r}, {self.line}:{self.col})"


KEYWORDS = {
    "source", "contract", "model", "pipeline", "fn", "import", "->", "=>",
    "from", "join_left", "join_inner", "join_anti", "join_semi", "on",
    "filter", "where", "let", "derive", "select", "aggregate", "group",
    "sort", "asc", "desc", "take", "all",
    "nonnull", "unique", "primary_key", "protected", "enum",
    "classification", "partition_by", "freshness",
    "nonnull",  # dedupe-safe
    "not", "and", "or", "in", "is", "null", "true", "false",
    "for", "over",
    "test", "expect",  # model-level declarative tests (Fase 5)
}

# type keywords map literal names -> builtin type
TYPE_KEYWORDS = {
    "int64", "float64", "decimal", "string", "bool", "date", "timestamp",
    "uuid", "json", "money", "array",
}

SYMBOLS = ["->", "==", "!=", "<=", ">=", "${", "=>", "||"]


class Lexer:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.line = 1
        self.col = 1
        self.tokens: List[Token] = []

    def _peek(self, off=0):
        i = self.pos + off
        return self.text[i] if i < len(self.text) else ""

    def _advance(self):
        ch = self.text[self.pos]
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def _skip_ws_and_comments(self):
        while self.pos < len(self.text):
            ch = self._peek()
            if ch in " \t\r\n":
                self._advance()
            elif ch == "/" and self._peek(1) == "/":
                while self.pos < len(self.text) and self._peek() != "\n":
                    self._advance()
            elif ch == "#":
                while self.pos < len(self.text) and self._peek() != "\n":
                    self._advance()
            elif ch == "/" and self._peek(1) == "*":
                self._advance(); self._advance()
                while self.pos < len(self.text) and not (
                    self._peek() == "*" and self._peek(1) == "/"
                ):
                    self._advance()
                if self.pos < len(self.text):
                    self._advance(); self._advance()
            else:
                break

    def _token(self, kind, value=None):
        self.tokens.append(Token(kind, value, self.line, self.col))

    def tokenize(self) -> List[Token]:
        while self.pos < len(self.text):
            self._skip_ws_and_comments()
            if self.pos >= len(self.text):
                break
            line, col = self.line, self.col
            ch = self._peek()

            if ch.isdigit() or (ch == "." and self._peek(1).isdigit()):
                self._number()
            elif ch == '"':
                self._string()
            elif ch == "$" and self._peek(1) == "{":
                # template interpolation is handled at string parse time;
                # a bare ${ only appears inside strings.
                self._advance(); self._advance()
                self._token("SYM", "${")
            elif ch.isalpha() or ch in "_":
                self._ident()
            elif ch in "{}[](),:;=.<>+-*/%":
                if any(self.text.startswith(s, self.pos) for s in SYMBOLS):
                    for s in sorted(SYMBOLS, key=len, reverse=True):
                        if self.text.startswith(s, self.pos):
                            for _ in s:
                                self._advance()
                            self._token("SYM", s)
                            break
                else:
                    self._advance()
                    self._token("SYM", ch)
            elif ch == "|":
                # lambda-pipe |x| handled as : in comprehension
                self._advance()
                self._token("SYM", "|")
            else:
                raise LexError(f"unexpected character {ch!r} at {line}:{col}")
        self.tokens.append(Token("EOF", None, self.line, self.col))
        return self.tokens

    def _number(self):
        line, col = self.line, self.col
        start = self.pos
        is_float = False
        while self.pos < len(self.text) and (self._peek().isdigit() or self._peek() == "_"):
            self._advance()
        if self._peek() == "." and self._peek(1).isdigit():
            is_float = True
            self._advance()
            while self.pos < len(self.text) and (self._peek().isdigit() or self._peek() == "_"):
                self._advance()
        raw = self.text[start:self.pos].replace("_", "")
        self.tokens.append(Token("INT" if not is_float else "FLOAT", raw, line, col))

    def _string(self):
        line, col = self.line, self.col
        self._advance()  # opening quote
        parts = []
        cur = []
        while True:
            if self.pos >= len(self.text):
                raise LexError(f"unterminated string at {line}:{col}")
            ch = self._peek()
            if ch == '"':
                self._advance()
                break
            if ch == "$" and self._peek(1) == "{":
                self._advance(); self._advance()
                if cur:
                    parts.append(("lit", "".join(cur)))
                    cur = []
                ident = []
                while self.pos < len(self.text) and (self._peek().isalnum() or self._peek() == "_"):
                    ident.append(self._advance())
                if not ident:
                    raise LexError(f"empty interpolation at {self.line}:{self.col}")
                if self._peek() != "}":
                    raise LexError(f"expected '}}' at {self.line}:{self.col}")
                self._advance()
                parts.append(("var", "".join(ident)))
            elif ch == "\\":
                self._advance()
                e = self._advance()
                cur.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(e, e))
            else:
                cur.append(self._advance())
        if cur:
            parts.append(("lit", "".join(cur)))
        self.tokens.append(Token("STR", parts, line, col))

    def _ident(self):
        line, col = self.line, self.col
        start = self.pos
        while self.pos < len(self.text) and (self._peek().isalnum() or self._peek() == "_"):
            self._advance()
        word = self.text[start:self.pos]
        if word in TYPE_KEYWORDS:
            self.tokens.append(Token("TYPE_KW", word, line, col))
        elif word in KEYWORDS:
            self.tokens.append(Token("KW", word, line, col))
        else:
            self.tokens.append(Token("ID", word, line, col))