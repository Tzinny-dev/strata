"""Recursive-descent parser matching spec/grammar.md."""
from __future__ import annotations

from typing import List, Optional, Tuple

from .lexer import Lexer, Token
from . import ast


class ParseError(Exception):
    def __init__(self, msg: str, file: str = "<strata>", line: int = 1,
                 col: int = 1, end_line: int = 1, end_col: int = 1) -> None:
        super().__init__(msg)
        self.file = file
        self.line = line
        self.col = col
        self.end_line = end_line
        self.end_col = end_col


PREC = {"or": 1, "and": 2, "cmp": 3, "add": 4, "mul": 5}

JOIN_KINDS = {"join_left": "left", "join_inner": "inner", "join_anti": "anti", "join_semi": "semi"}

MODEL_ATTRS = {"owner", "reason", "description", "label", "staleness_ok"}


class Parser:
    def __init__(self, text: str, path: str = "<strata>") -> None:
        self.ts = Lexer(text, path=path).tokenize()
        self.i = 0
        self.path = path

    # ------------------------------------------------------------ token helpers
    def cur(self) -> Token:
        """Return the current token (never past EOF)."""
        return self.ts[self.i]

    def peek(self, n: int = 1) -> Token:
        """Look at the token n positions ahead without consuming it."""
        j = min(self.i + n, len(self.ts) - 1)
        return self.ts[j]

    def advance(self) -> Token:
        """Consume and return the current token (EOF is sticky)."""
        t = self.ts[self.i]
        if t.kind != "EOF":
            self.i += 1
        return t

    def at(self, kind: str, value: Optional[object] = None) -> bool:
        """True when the current token has the given kind and optional value."""
        t = self.cur()
        return t.kind == kind and (value is None or t.value == value)

    def match(self, kind: str, value: Optional[object] = None) -> Optional[Token]:
        """Consume and return the current token when it matches kind/value, else None."""
        if self.at(kind, value):
            return self.advance()
        return None

    def expect(self, kind: str, value: Optional[object] = None) -> Token:
        """Consume a token of the given kind/value or raise ParseError with a span."""
        t = self.cur()
        if (value is None and t.kind == kind) or (value is not None and t.kind == kind and t.value == value):
            return self.advance()
        raise ParseError(
            f"expected {kind} {value or ''} but found {t.kind} {t.value!r}",
            file=self.path, line=t.line, col=t.col,
            end_line=t.end_line, end_col=t.end_col)

    def span(self, tok: Token) -> Tuple[int, int, int, int]:
        """Source span tuple (line, col, end_line, end_col) of a token."""
        return (tok.line, tok.col, tok.end_line, tok.end_col)

    # ------------------------------------------------------------ top level
    def parse_module(self) -> ast.Module:
        """Entry point: parse the whole token stream into a Module."""
        m = ast.Module(path=self.path)
        while not self.at("EOF"):
            if self.at("KW", "import"):
                self.advance()
                parts = [self.expect("ID").value]
                while self.match("SYM", "."):
                    parts.append(self.expect("ID").value)
                m.decls.append(ast.ImportDecl(path=".".join(parts)))
            elif self.at("KW", "source"):
                m.decls.append(self.parse_source())
            elif self.at("KW", "contract"):
                m.decls.append(self.parse_contract())
            elif self.at("KW", "domain"):
                m.decls.append(self.parse_domain())
            elif self.at("KW", "model"):
                m.decls.append(self.parse_model_decl())
            elif self.at("KW", "fn"):
                m.decls.append(self.parse_fn())
            elif self.at("KW", "pipeline"):
                m.decls.append(self.parse_pipeline())
            elif self.at("KW", "test"):
                m.decls.append(self.parse_test())
            elif self.at("ID") and self.peek().value == "(":
                # top-level generator call: fn(args) -> List<Model>
                m.decls.append(ast.GeneratorDecl(call=self.parse_primary(), span=self.span(self.cur())))
            else:
                t = self.cur()
                raise ParseError(
                    f"expected top-level declaration "
                    f"(source|contract|model|fn|pipeline|import|domain) but found {t.value!r}",
                    file=self.path, line=t.line, col=t.col,
                    end_line=t.end_line, end_col=t.end_col)
        return m

    def parse_test(self) -> ast.TestDecl:
        """`test <model> { expect <col> <op> <literal>; ... }` — declarative
        per-model data tests (Fase 5). The rhs is a literal (typed at compile
        time); `expect row_count == N` is the reserved aggregate form."""
        kw = self.expect("KW", "test")
        decl = ast.TestDecl(span=self.span(kw))
        decl.model = self.expect("ID").value
        self.expect("SYM", "{")
        while not self.at("SYM", "}"):
            self.expect("KW", "expect")
            chk = ast.TestCheck(span=self.span(self.cur()))
            name = self.expect("ID").value
            if name == "row_count":
                chk.kind = "row_count"
            else:
                chk.kind, chk.col = "expect", name
            chk.op = {"==": "==", "!=": "!=", ">": ">", "<": "<",
                      ">=": ">=", "<=": "<="}[self.expect("SYM").value]
            t = self.cur()
            if self.at("INT"):
                chk.value = int(self.advance().value)
            elif self.at("FLOAT"):
                chk.value = float(self.advance().value)
            elif self.at("STR"):
                chk.value = "".join(p[1] for p in self.advance().value)
            elif self.match("KW", "true") or self.match("KW", "false"):
                chk.value = t.value == "true"
            elif self.match("KW", "null"):
                chk.value = None
            else:
                t = self.cur()
                raise ParseError(
                    f"expected literal after "
                    f"'{chk.col or chk.kind} {chk.op}' in expect",
                    file=self.path, line=t.line, col=t.col,
                    end_line=t.end_line, end_col=t.end_col)
            if not self.match("SYM", ";"):
                if not (self.at("KW", "expect") or self.at("SYM", "}")):
                    self.expect("SYM", ";")
            decl.checks.append(chk)
        self.expect("SYM", "}")
        return decl

    def parse_source(self) -> ast.SourceDecl:
        """Parse a `source` declaration."""
        kw = self.expect("KW", "source")
        decl = ast.SourceDecl(span=self.span(kw))
        decl.name = self.expect("ID").value
        self.expect("SYM", "(")
        if not self.at("SYM", ")"):
            while True:
                k = self.expect("ID").value
                self.expect("SYM", ":")
                if self.at("STR"):
                    v = "".join(p[1] for p in self.advance().value)  # literal parts only
                else:
                    v = self.expect("ID").value
                decl.resource[k] = v
                if not self.match("SYM", ","):
                    break
        self.expect("SYM", ")")
        if self.match("SYM", "{"):
            decl.props = self.parse_source_props()
            self.expect("SYM", "}")
        return decl

    def parse_source_props(self) -> List[Tuple[str, object]]:
        """Parse the resource property list of a source declaration."""
        props = []
        while not self.at("SYM", "}"):
            k = self.cur().value
            if k == "columns" and self.peek().value == ":" and self.peek(2).value == "{":
                self.advance()  # columns
                self.expect("SYM", ":")
                self.expect("SYM", "{")
                fields = []
                while not self.at("SYM", "}"):
                    fields.append(self.parse_contract_field())
                    self.match("SYM", ",")
                    if self.at("SYM", ";"):
                        self.advance()
                self.expect("SYM", "}")
                props.append(("columns", fields))
                self.match("SYM", ",")
                continue
            self.advance()  # key
            self.expect("SYM", ":")
            if self.at("STR"):
                v = "".join(p[1] for p in self.advance().value)
            elif self.at("INT"):
                v = int(self.advance().value)
            else:
                v = self.advance().value
            props.append((k, v))
            self.match("SYM", ",")
        return props

    def parse_contract(self) -> ast.ContractDecl:
        """Parse a `contract` declaration."""
        kw = self.expect("KW", "contract")
        decl = ast.ContractDecl(span=self.span(kw))
        decl.name = self.expect("ID").value
        self.expect("SYM", "{")
        while not self.at("SYM", "}"):
            f = self.parse_contract_field()
            decl.fields.append(f)
            self.match("SYM", ",")
            if not self.at("SYM", "}") and self.at("SYM", ";"):
                self.advance()
        self.expect("SYM", "}")
        return decl

    def parse_domain(self) -> ast.DomainDecl:
        """Parse a `domain` declaration."""
        kw = self.expect("KW", "domain")
        decl = ast.DomainDecl(span=self.span(kw))
        decl.name = self.expect("ID").value
        self.expect("SYM", "=")
        decl.type_spec, decl.params = self.parse_type_spec()
        return decl

    def parse_type_spec(self) -> Tuple[str, List[object]]:
        """A type in a source/contract/domain position: a builtin (with
        parameters, array elements recursing to any depth), or a bare
        identifier referencing a `domain` alias (resolved at check time).

        Returns (spec, params); a bare name without parameters stays a bare
        string so simple types keep their historical AST shape.
        """
        tok = self.cur()
        if tok.kind == "ID":
            return self.advance().value, []
        if tok.kind != "TYPE_KW":
            raise ParseError(
                f"expected type but found {tok.kind} {tok.value!r}",
                file=self.path, line=tok.line, col=tok.col,
                end_line=tok.end_line, end_col=tok.end_col)
        spec = self.advance().value
        if spec == "decimal":
            self.expect("SYM", "(")
            params = [int(self.expect("INT").value)]
            self.match("SYM", ",")
            params.append(int(self.expect("INT").value))
            self.expect("SYM", ")")
            return spec, params
        if spec == "array":
            self.expect("SYM", "(")
            espec, eparams = self.parse_type_spec()
            self.expect("SYM", ")")
            elem = espec if eparams == [] else (espec, eparams)
            return spec, [elem]
        if spec == "money":
            if self.match("SYM", "("):
                cur = self.expect("ID").value
                self.expect("SYM", ")")
                return spec, [cur]
            return spec, []
        return spec, []

    def parse_contract_field(self) -> ast.ContractField:
        """Parse a single contract field."""
        f = ast.ContractField()
        f.name = self.expect("ID").value
        self.expect("SYM", ":")
        f.type_spec, f.params = self.parse_type_spec()
        # annotations
        while True:
            if self.match("KW", "nonnull"):
                f.nonnull = True
            elif self.match("KW", "unique"):
                f.unique = True
            elif self.match("KW", "primary_key"):
                f.primary = True
                f.unique = True
                f.nonnull = True
            elif self.match("KW", "protected"):
                f.protected = True
            elif self.at("KW", "enum"):
                self.advance()
                self.expect("SYM", "{")
                vals = []
                while not self.at("SYM", "}"):
                    if self.at("EOF"):
                        t = self.cur()
                        raise ParseError(
                            "unterminated enum values (expected '}')",
                            file=self.path, line=t.line, col=t.col,
                            end_line=t.end_line, end_col=t.end_col)
                    if self.at("STR"):
                        vals.append("".join(p[1] for p in self.advance().value))
                    else:
                        vals.append(self.advance().value)
                    self.match("SYM", ",")
                self.expect("SYM", "}")
                f.enum = vals
            elif self.at("KW", "classification"):
                self.advance()
                self.expect("SYM", ":")
                if self.at("STR"):
                    f.classification = "".join(p[1] for p in self.advance().value)
                else:
                    f.classification = self.expect("ID").value
            else:
                break
        return f

    def _parse_freshness_value(self) -> List[str]:
        """Parse freshness value(s): incremental, daily, weekly, 1h, 24h, etc.

        Supports comma-separated multiple values: freshness 1h, daily
        Supports custom expressions: freshness "now() - interval '1 day'"
        """
        values = []
        # Parse first value
        if self.at("INT"):
            num = self.advance().value
            if self.at("ID"):
                unit = self.advance().value
                values.append(f"{num}{unit}")
            else:
                values.append(num)
        elif self.at("STR"):
            # Custom expression as string
            values.append("".join(p[1] for p in self.advance().value))
        elif self.at("KW", "incremental"):
            # 'incremental' is a keyword but used as freshness value
            self.advance()
            values.append("incremental")
        else:
            values.append(self.expect("ID").value)
        # Check for comma-separated values
        while self.match("SYM", ","):
            if self.at("INT"):
                num = self.advance().value
                if self.at("ID"):
                    unit = self.advance().value
                    values.append(f"{num}{unit}")
                else:
                    values.append(num)
            elif self.at("STR"):
                # Custom expression as string
                values.append("".join(p[1] for p in self.advance().value))
            elif self.at("KW", "incremental"):
                # 'incremental' is a keyword but used as freshness value
                self.advance()
                values.append("incremental")
            else:
                values.append(self.expect("ID").value)
        return values

    def parse_model_decl(self) -> ast.ModelDecl:
        """Parse a `model` declaration."""
        kw = self.expect("KW", "model")
        decl = ast.ModelDecl(span=self.span(kw))
        name_tok = self.cur()
        if self.at("ID"):
            decl.name = self.advance().value
        elif self.at("STR"):
            decl.name = "".join(p[1] for p in self.advance().value)
        else:
            raise ParseError("expected model name",
                             file=self.path, line=name_tok.line,
                             col=name_tok.col, end_line=name_tok.end_line,
                             end_col=name_tok.end_col)
        if self.match("SYM", "->"):
            self.expect("KW", "contract")
            decl.contract = self.expect("ID").value
        self.expect("SYM", "{")
        while not self.at("SYM", "}"):
            if self.at("ID") and self.peek().value == ":" and self.cur().value in MODEL_ATTRS:
                k = self.advance().value
                self.advance()  # ':'
                if not self.at("STR"):
                    t = self.cur()
                    raise ParseError(
                        f"expected string value for model attribute {k!r}",
                        file=self.path, line=t.line, col=t.col,
                        end_line=t.end_line, end_col=t.end_col)
                v = "".join(p[1] for p in self.advance().value)  # STR
                decl.attrs[k] = v
            elif self.at("KW", "from"):
                t = self.advance()
                decl.stmts.append(ast.FromStmt(table=self.expect("ID").value, span=self.span(t)))
            elif self.at("KW", "partition_by"):
                t = self.advance()
                decl.partition_by = []
                self.expect("SYM", "[")
                if not self.at("SYM", "]"):
                    while True:
                        decl.partition_by.append(self.parse_expr())
                        if not self.match("SYM", ","):
                            break
                self.expect("SYM", "]")
                # After partition_by, check for freshness
                if self.at("KW", "freshness"):
                    t = self.advance()
                    if self.at("SYM", ":"):
                        self.advance()
                    decl.freshness = self._parse_freshness_value()
                    # Check for freshness_column
                    if self.at("KW", "freshness_column"):
                        self.advance()
                        self.expect("SYM", ":")
                        decl.freshness_column = self.expect("ID").value
            elif self.at("KW", "freshness"):
                t = self.advance()
                # freshness: <value>
                if self.at("SYM", ":"):
                    self.advance()
                    decl.freshness = self._parse_freshness_value()
                else:
                    # Maybe it's just a keyword without colon
                    decl.freshness = self._parse_freshness_value()
                # Check for freshness_column
                if self.at("KW", "freshness_column"):
                    self.advance()
                    self.expect("SYM", ":")
                    decl.freshness_column = self.expect("ID").value
                # Check for incremental configuration
                if self.at("KW", "incremental"):
                    self.advance()
                    decl.incremental = True
                    # Check for merge_strategy
                    if self.at("KW", "merge_strategy"):
                        self.advance()
                        self.expect("SYM", ":")
                        decl.merge_strategy = self.expect("ID").value
                    # Check for merge_keys
                    if self.at("KW", "merge_keys"):
                        self.advance()
                        self.expect("SYM", ":")
                        self.expect("SYM", "[")
                        if not self.at("SYM", "]"):
                            while True:
                                decl.merge_keys.append(self.parse_expr())
                                if not self.match("SYM", ","):
                                    break
                        self.expect("SYM", "]")
                    # Check for cdc_column
                    if self.at("KW", "cdc_column"):
                        self.advance()
                        self.expect("SYM", ":")
                        decl.cdc_column = self.expect("ID").value
            elif self.at("KW", "incremental"):
                # Incremental model without freshness
                self.advance()
                decl.incremental = True
                # Check for merge_strategy
                if self.at("KW", "merge_strategy"):
                    self.advance()
                    self.expect("SYM", ":")
                    decl.merge_strategy = self.expect("ID").value
                # Check for merge_keys
                if self.at("KW", "merge_keys"):
                    self.advance()
                    self.expect("SYM", ":")
                    self.expect("SYM", "[")
                    if not self.at("SYM", "]"):
                        while True:
                            decl.merge_keys.append(self.parse_expr())
                            if not self.match("SYM", ","):
                                break
                    self.expect("SYM", "]")
                # Check for cdc_column
                if self.at("KW", "cdc_column"):
                    self.advance()
                    self.expect("SYM", ":")
                    decl.cdc_column = self.expect("ID").value
            elif self.at("KW") and self.cur().value in JOIN_KINDS:
                k, t = self.cur().value, self.advance()
                decl.stmts.append(ast.JoinStmt(
                    kind=JOIN_KINDS[k], table=self.expect("ID").value, span=self.span(t)))
                self.expect("KW", "on")
                decl.stmts[-1].on = self.parse_expr()
                decl.stmts[-1].expect = self.parse_join_expect()
            elif self.at("KW") and self.cur().value in ("filter", "where"):
                t = self.advance()
                decl.stmts.append(ast.FilterStmt(cond=self.parse_expr(), span=self.span(t)))
            elif self.at("KW", "let"):
                t = self.advance()
                name = self.expect("ID").value
                self.expect("SYM", "=")
                decl.stmts.append(ast.LetStmt(name=name, expr=self.parse_expr(), span=self.span(t)))
            elif self.at("KW", "derive"):
                t = self.advance()
                decl.stmts.append(ast.DeriveStmt(assigns=self.parse_assigns(), span=self.span(t)))
            elif self.at("KW", "aggregate"):
                t = self.advance()
                decl.stmts.append(ast.AggregateStmt(assigns=self.parse_assigns(), span=self.span(t)))
            elif self.at("KW", "group"):
                t = self.advance()
                self.expect("SYM", "{")
                keys = []
                while not self.at("SYM", "}"):
                    keys.append(self.parse_expr())
                    if not self.match("SYM", ","):
                        break
                self.expect("SYM", "}")
                self.expect("SYM", "(")
                body = []
                while not self.at("SYM", ")"):
                    if not self.at("KW"):
                        t = self.cur()
                        raise ParseError("expected statement",
                                         file=self.path, line=t.line,
                                         col=t.col, end_line=t.end_line,
                                         end_col=t.end_col)
                    k = self.cur().value
                    if k in ("filter", "where"):
                        tt = self.advance()
                        body.append(ast.FilterStmt(cond=self.parse_expr(), span=self.span(tt)))
                    elif k == "aggregate":
                        tt = self.advance()
                        body.append(ast.AggregateStmt(assigns=self.parse_assigns(), span=self.span(tt)))
                    elif k == "sort":
                        tt = self.advance()
                        body.append(self.parse_sort(tt))
                    elif k == "take":
                        tt = self.advance()
                        body.append(self.parse_take(tt))
                    else:
                        t = self.cur()
                        raise ParseError(
                            f"unexpected {k} in group body",
                            file=self.path, line=t.line, col=t.col,
                            end_line=t.end_line, end_col=t.end_col)
                self.expect("SYM", ")")
                decl.stmts.append(ast.GroupStmt(keys=keys, body=body, span=self.span(t)))
            elif self.at("KW", "sort"):
                t = self.advance()
                decl.stmts.append(self.parse_sort(t))
            elif self.at("KW", "take"):
                t = self.advance()
                decl.stmts.append(self.parse_take(t))
            elif self.at("KW", "expand"):
                t = self.advance()
                name = self.expect("ID").value
                as_name = name
                if self.at("ID", "as"):
                    self.advance()
                    as_name = self.expect("ID").value
                decl.stmts.append(ast.ExpandStmt(name=name, as_name=as_name, span=self.span(t)))
            elif self.at("KW", "union") or self.at("KW", "intersect") or self.at("KW", "except"):
                t = self.advance()
                op = t.value
                all_ = False
                if op == "union" and self.at("KW", "all"):
                    self.advance()
                    all_ = True
                decl.stmts.append(ast.SetOpStmt(op=op, table=self.expect("ID").value,
                                                all=all_, span=self.span(t)))
            elif self.at("KW", "dedup"):
                t = self.advance()
                by = []
                if self.match("KW", "by"):
                    by.append(self.parse_expr())
                    while self.match("SYM", ","):
                        by.append(self.parse_expr())
                decl.stmts.append(ast.DedupStmt(by=by, span=self.span(t)))
            elif self.at("KW", "select"):
                t = self.advance()
                decl.stmts.append(ast.SelectStmt(assigns=self.parse_assigns(), span=self.span(t)))
            else:
                t = self.cur()
                raise ParseError(
                    f"unexpected token {t.value!r} in model body",
                    file=self.path, line=t.line, col=t.col,
                    end_line=t.end_line, end_col=t.end_col)
        self.expect("SYM", "}")
        return decl

    def parse_join_expect(self) -> Optional[str]:
        """Parse an optional `expect ...` marker after a join key."""
        # `expect many_to_one|one_to_one` after a join condition. The two
        # cardinalities are contextual identifiers (not keywords) so existing
        # columns named like them keep parsing; anything else is a parse error.
        if not self.match("KW", "expect"):
            return None
        if self.at("ID") and self.cur().value in ("many_to_one", "one_to_one"):
            return self.advance().value
        t = self.cur()
        raise ParseError(
            f"expected join cardinality many_to_one or one_to_one "
            f"but found {t.kind} {t.value!r}",
            file=self.path, line=t.line, col=t.col,
            end_line=t.end_line, end_col=t.end_col)

    def parse_sort(self, t: Token) -> ast.SortStmt:
        """Parse a `sort` statement (may be asc)."""
        self.expect("SYM", "{")
        keys = []
        while not self.at("SYM", "}"):
            e = self.parse_expr()
            desc = False
            if self.match("KW", "desc"):
                desc = True
            elif self.match("KW", "asc"):
                desc = False
            keys.append((e, desc))
            if not self.match("SYM", ","):
                break
        self.expect("SYM", "}")
        return ast.SortStmt(keys=keys, span=self.span(t))

    def parse_take(self, t: Token) -> ast.TakeStmt:
        """Parse a `take` / `take_last` statement."""
        st = ast.TakeStmt(span=self.span(t))
        st.start = int(self.expect("INT").value)
        if self.match("SYM", ".") and self.match("SYM", "."):
            st.end = int(self.expect("INT").value)
        else:
            st.limit = st.start
        return st

    def parse_assigns(self) -> List[ast.OutAssign]:
        """Parse the `assign { ... }` block of a transform step."""
        self.expect("SYM", "{")
        assigns = []
        while not self.at("SYM", "}"):
            name = self.expect("ID").value
            self.expect("SYM", "=")
            assigns.append(ast.OutAssign(name=name, expr=self.parse_expr()))
            if not self.match("SYM", ","):
                break
        self.expect("SYM", "}")
        return assigns

    # ------------------------------------------------------------ fn decl
    def parse_fn(self) -> ast.FnDecl:
        """Parse a `fn` declaration."""
        kw = self.expect("KW", "fn")
        decl = ast.FnDecl(span=self.span(kw))
        decl.name = self.expect("ID").value
        self.expect("SYM", "(")
        if not self.at("SYM", ")"):
            while True:
                pname = self.expect("ID").value
                self.expect("SYM", ":")
                decl.params.append((pname, "".join(self.parse_type_str())))
                if not self.match("SYM", ","):
                    break
        self.expect("SYM", ")")
        self.expect("SYM", "->")
        decl.return_type = "".join(self.parse_type_str())
        braced = bool(self.match("SYM", "{"))
        decl.body = self.parse_expr()
        if braced:
            self.expect("SYM", "}")
        return decl

    def parse_type_str(self) -> List[str]:
        """Parse a type expression (`List<...>`, primitive, or model reference)."""
        if self.at("ID") and self.cur().value == "List":
            self.advance()
            self.expect("SYM", "<")
            inner = self.parse_type_str()
            self.expect("SYM", ">")
            return ["List<"] + inner + [">"]
        if self.at("TYPE_KW") or self.at("ID"):
            self.advance()
            return [self.ts[self.i - 1].value]
        raise ParseError("expected type",
                         file=self.path, line=self.cur().line,
                         col=self.cur().col, end_line=self.cur().end_line,
                         end_col=self.cur().end_col)

    # ------------------------------------------------------------ expressions
    def parse_expr(self, min_prec: int = 0) -> ast.Node:
        """Parse an infix expression down to the given minimum precedence."""
        left = self.parse_unary()
        while True:
            op = self.cur().value if self.cur().kind in ("KW", "SYM") else None
            prec = self._prec(op)
            if op is None or prec < min_prec:
                break
            # comparisons are non-associative in SQL-like langs; treat left-assoc
            self.advance()
            right = self.parse_expr(prec + 1)
            left = ast.BinOp(op=op, left=left, right=right)
        return left

    def _prec(self, op: Optional[object]) -> int:
        if op in ("==", "!=", "<", "<=", ">", ">=", "in"):
            return PREC["cmp"]
        if op in ("+", "-", "||"):
            return PREC["add"]
        if op in ("*", "/", "%"):
            return PREC["mul"]
        if op == "and":
            return PREC["and"]
        if op == "or":
            return PREC["or"]
        return -1

    def parse_unary(self) -> ast.Node:
        """Parse a unary (NOT / negation) expression."""
        t = self.cur()
        if self.at("KW", "not") or self.at("SYM", "-"):
            op = self.advance().value
            return ast.UnOp(op=op, operand=self.parse_unary(), span=self.span(t))
        return self.parse_primary()

    def parse_primary(self) -> ast.Node:
        """Parse a primary expression: literal, call, list, column ref, or group."""
        t = self.cur()
        span = self.span(t)

        if t.kind == "INT":
            self.advance()
            return ast.Literal(value=int(t.value), span=span)
        if t.kind == "FLOAT":
            self.advance()
            return ast.Literal(value=float(t.value), span=span)
        if t.kind == "STR":
            self.advance()
            if all(k == "lit" for k, _ in t.value):
                return ast.Literal(value="".join(v for _, v in t.value), span=span)
            return ast.TemplateStr(parts=t.value, span=span)
        if t.kind == "KW" and t.value == "true":
            self.advance(); return ast.Literal(value=True, span=span)
        if t.kind == "KW" and t.value == "false":
            self.advance(); return ast.Literal(value=False, span=span)
        if t.kind == "KW" and t.value == "null":
            self.advance(); return ast.Literal(value=None, span=span)
        if self.at("SYM", "["):
            self.advance()
            return self.parse_list(span)
        if self.at("SYM", "("):
            self.advance()
            e = self.parse_expr()
            self.expect("SYM", ")")
            return e
        if self.at("SYM", "*"):
            # `*` in primary position: only meaningful as count(*) — the
            # typechecker rejects a stray one (E064), so multiplication (which
            # parses this position as its right operand) is unaffected.
            self.advance()
            return ast.Star(span=span)
        if self.at("KW", "model"):
            return self.parse_model_value()

        # call / ref / qualified ref
        if t.kind == "ID":
            self.advance()
            if self.at("SYM", "."):
                self.advance()
                col = self.expect("ID").value
                return ast.ColumnRef(name=col, qualifier=t.value, span=span)
            if self.at("SYM", "("):
                args = []
                distinct = False
                self.advance()
                if self.match("KW", "distinct"):
                    # `count(distinct x)` — the single DISTINCT aggregate form.
                    distinct = True
                if not self.at("SYM", ")"):
                    while True:
                        # Keyword values remain ordinary expressions. Both
                        # argument forms share the comma/closing-paren path.
                        if self.at("ID") and self.peek(1).kind == "SYM" and self.peek(1).value == ":":
                            key_tok = self.advance()
                            self.advance()  # ':'
                            args.append(ast.Kwarg(name=key_tok.value, value=self.parse_expr(),
                                                  span=self.span(key_tok)))
                        else:
                            arg = self.parse_expr()
                            # Only the unit slot interprets bare identifiers
                            # symbolically; columns named day/month elsewhere
                            # retain their ordinary meaning.
                            unit_slot = ((t.value == "date_trunc" and len(args) == 1)
                                         or (t.value == "date_diff" and len(args) == 2))
                            if unit_slot and isinstance(arg, ast.ColumnRef) and arg.qualifier is None:
                                arg = ast.Literal(value=arg.name, span=arg.span)
                            args.append(arg)
                        if not self.match("SYM", ","):
                            break
                self.expect("SYM", ")")
                if self.at("KW", "over"):
                    if distinct:
                        # count(distinct x) over (...): parse but the checker
                        # rejects it (DISTINCT aggregation is plain-only).
                        win = self.parse_window_call(t.value, args, span)
                        win.distinct = True
                        return win
                    return self.parse_window_call(t.value, args, span)
                return ast.Call(name=t.value, args=args, distinct=distinct, span=span)
            return ast.ColumnRef(name=t.value, span=span)

        raise ParseError(
            f"unexpected {t.value!r} in expression",
            file=self.path, line=t.line, col=t.col,
            end_line=t.end_line, end_col=t.end_col)

    def parse_window_call(self, name: str, args: List[ast.Node],
                          span: Tuple[int, int, int, int]) -> ast.WindowCall:
        """`fn(args) over (partition_by: [...], sort: [expr desc, ...])`.

        `over` is a reserved window keyword, never a bare call name; both
        clauses are optional but the parentheses are not.
        """
        self.advance()  # over
        self.expect("SYM", "(")
        spec = ast.WindowSpec(span=self.span(self.cur()))
        if not self.at("SYM", ")"):
            # `over ()` (no clauses) is legal SQL: the frame is the whole set.
            self._window_clause(spec)
            if self.match("SYM", ","):
                self._window_clause(spec)
        self.expect("SYM", ")")
        return ast.WindowCall(name=name, args=args, over=spec, span=span)

    def _window_clause(self, spec: ast.WindowSpec) -> None:
        kw = self.expect("ID" if self.at("ID") else "KW").value
        if kw == "partition_by":
            self.expect("SYM", ":")
            self.expect("SYM", "[")
            if not self.at("SYM", "]"):
                while True:
                    spec.partition_by.append(self.parse_expr())
                    if not self.match("SYM", ","):
                        break
            self.expect("SYM", "]")
        elif kw == "sort":
            self.expect("SYM", ":")
            self.expect("SYM", "[")
            if not self.at("SYM", "]"):
                while True:
                    key = self.parse_expr()
                    spec.sort.append((key, bool(self.match("KW", "desc"))))
                    if not self.match("SYM", ","):
                        break
            self.expect("SYM", "]")
        else:
            t = self.cur()
            raise ParseError(
                f"expected 'partition_by:' or 'sort:' in over(...), found {kw!r}",
                file=self.path, line=t.line, col=t.col,
                end_line=t.end_line, end_col=t.end_col)

    def parse_list(self, span: Tuple[int, int, int, int]) -> ast.Node:
        """Parse a list literal [...]."""
        # [ ... ] or [ body for var in iter ]
        first = None
        if not self.at("SYM", "]"):
            first = self.parse_expr()
        if self.match("KW", "for"):
            var = self.expect("ID").value
            self.expect("KW", "in")
            it = self.parse_expr()
            self.expect("SYM", "]")
            return ast.ListComprehension(body=first, var=var, iterable=it, span=span)
        items = [first] if first is not None else []
        while self.match("SYM", ","):
            if self.at("SYM", "]"):
                break
            items.append(self.parse_expr())
        self.expect("SYM", "]")
        return ast.ListExpr(items=items, span=span)

    def parse_model_value(self) -> ast.ModelValue:
        """Parse a `model value` expression."""
        t = self.expect("KW", "model")
        mv = ast.ModelValue(span=self.span(t))
        name_tok = self.cur()
        if self.at("ID"):
            mv.name = ast.Literal(value=self.advance().value)
        elif self.at("STR"):
            tok = self.advance()
            if all(k == "lit" for k, _ in tok.value):
                mv.name = ast.Literal(value="".join(v for _, v in tok.value))
            else:
                mv.name = ast.TemplateStr(parts=tok.value)
        else:
            raise ParseError("expected model name",
                             file=self.path, line=name_tok.line,
                             col=name_tok.col, end_line=name_tok.end_line,
                             end_col=name_tok.end_col)
        if self.match("SYM", "->"):
            self.expect("KW", "contract")
            mv.contract = self.expect("ID").value
        self.expect("SYM", "{")
        while not self.at("SYM", "}"):
            if self.at("ID") and self.peek().value == ":" and self.cur().value in MODEL_ATTRS:
                k = self.advance().value
                self.advance()
                if not self.at("STR"):
                    t = self.cur()
                    raise ParseError(
                        f"expected string value for model attribute {k!r}",
                        file=self.path, line=t.line, col=t.col,
                        end_line=t.end_line, end_col=t.end_col)
                mv.attrs[k] = "".join(p[1] for p in self.advance().value)
            elif self.at("KW", "from"):
                tt = self.advance()
                mv.stmts.append(ast.FromStmt(table=self.expect("ID").value, span=self.span(tt)))
            elif self.at("KW") and self.cur().value in JOIN_KINDS:
                k, tt = self.cur().value, self.advance()
                mv.stmts.append(ast.JoinStmt(
                    kind=JOIN_KINDS[k], table=self.expect("ID").value, span=self.span(tt)))
                self.expect("KW", "on")
                mv.stmts[-1].on = self.parse_expr()
                mv.stmts[-1].expect = self.parse_join_expect()
            elif self.at("KW") and self.cur().value in ("filter", "where"):
                tt = self.advance()
                mv.stmts.append(ast.FilterStmt(cond=self.parse_expr(), span=self.span(tt)))
            elif self.at("KW", "let"):
                tt = self.advance()
                n = self.expect("ID").value
                self.expect("SYM", "=")
                mv.stmts.append(ast.LetStmt(name=n, expr=self.parse_expr(), span=self.span(tt)))
            elif self.at("KW", "derive"):
                tt = self.advance()
                mv.stmts.append(ast.DeriveStmt(assigns=self.parse_assigns(), span=self.span(tt)))
            elif self.at("KW", "take"):
                tt = self.advance()
                mv.stmts.append(self.parse_take(tt))
            else:
                t = self.cur()
                raise ParseError(
                    f"unexpected token {t.value!r} in fn model body",
                    file=self.path, line=t.line, col=t.col,
                    end_line=t.end_line, end_col=t.end_col)
        self.expect("SYM", "}")
        return mv

    def parse_pipeline(self) -> ast.PipelineDecl:
        """Parse a `pipeline` declaration."""
        kw = self.expect("KW", "pipeline")
        decl = ast.PipelineDecl(span=self.span(kw))
        decl.name = self.expect("ID").value
        if self.at("ID") and self.cur().value == "env":
            self.advance()
            self.expect("SYM", ":")
            decl.env = self.expect("ID").value
        self.expect("SYM", "{")
        while not self.at("SYM", "}"):
            k = self.expect("ID").value
            self.expect("SYM", ":")
            if k == "models":
                self.expect("SYM", "[")
                items = []
                while not self.at("SYM", "]"):
                    items.append(self.parse_expr())
                    if not self.match("SYM", ","):
                        break
                self.expect("SYM", "]")
                decl.models = items
            elif k == "env":
                decl.env = self.expect("ID").value
            elif k == "sources":
                self.expect("SYM", "{")
                while not self.at("SYM", "}"):
                    src = self.expect("ID").value
                    if self.at("SYM", ":"):
                        self.advance()
                        fn = self.advance().value  # 'from'
                        self.expect("SYM", "(")
                        kv = {}
                        while not self.at("SYM", ")"):
                            kk = self.expect("ID").value
                            self.expect("SYM", ":")
                            if self.at("STR"):
                                kv[kk] = "".join(p[1] for p in self.advance().value)
                            else:
                                kv[kk] = self.advance().value
                            self.match("SYM", ",")
                        self.expect("SYM", ")")
                        decl.sources[src] = kv
                    self.match("SYM", ",")
                self.expect("SYM", "}")
            else:
                t = self.cur()
                raise ParseError(
                    f"unknown pipeline key {k!r}",
                    file=self.path, line=t.line, col=t.col,
                    end_line=t.end_line, end_col=t.end_col)
            self.match("SYM", ",")
        self.expect("SYM", "}")
        return decl


def parse_strata(text: str, path: str = "<strata>") -> ast.Module:
    """Parse Strata source text into a Module AST."""
    return Parser(text, path).parse_module()