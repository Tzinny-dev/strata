"""Strata grammar-as-code: single source for the GBNF grammar emitted by `strata grammar`.

Derived from spec/grammar.md (EBNF) with deviations resolved toward the reference
parser (strata/parser.py), which is authoritative per the spec header note:

- sort_stmt uses `asc`/`desc` keywords (parser.parse_sort), NOT the EBNF `-expr`.
- take_stmt is `INT [ ".." INT ]` (parser.parse_take) — the `| "take" INT` /
  `[INT ".."] [INT]` EBNF alternates never parse in the reference implementation.
- from_stmt table is a plain IDENT (no `schema.` qualification parsed).
- contract_field type is a single TYPE_KW, `array` is parameterized `array(TYPE_KW)`
  (no `<...>` form), `money` may carry `(IDENT)`.
- source_prop supports `columns: { contract_field { ";" | "," } }` (parser.parse_source_props).
- literals: `null` (not `none`), plus list `[ expr { "," expr } ]` and
  `${expr}` template substitution inside strings.
- model_decl attrs are `label: STR` (parser MODEL_ATTRS), not `reason: STR`.
- fn_decl: `{ fn_body }` braces are optional and only wrap a single expr;
  `case`/`dict`/`map` are accepted as generic calls (parser.parse_primary), not
  reserved call names.

Note on newlines: the lexer emits no NEWLINE tokens and the parser is
whitespace-insensitive (statement boundaries are keyword/brace driven), so this
grammar is whitespace-agnostic too. GBNF target: llama.cpp grammar engine.

Invariant: every keyword/tok column below MUST be a KEYWORDS/TYPE_KEYWORDS/
SYMBOLS member of strata/lexer.py — tests/test_grammar.py enforces it, so this
table cannot drift from the real lexer.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# lexical terminals shared with lexer.py (kept in lockstep, enforced by tests)
# ---------------------------------------------------------------------------
KEYWORDS: List[str] = [
    "source", "contract", "model", "pipeline", "fn", "import", "from",
    "join_left", "join_inner", "join_anti", "join_semi", "on",
    "filter", "where", "let", "derive", "select", "aggregate", "group",
    "sort", "asc", "desc", "take", "all", "expand",
    "nonnull", "unique", "primary_key", "protected", "enum",
    "classification", "partition_by", "freshness",
    "not", "and", "or", "in", "is", "null", "true", "false",
    "for", "over", "->", "=>", "test", "expect",
]

TYPE_KEYWORDS: List[str] = [
    "int64", "float64", "decimal", "string", "bool", "date", "timestamp",
    "uuid", "json", "money", "array",
]

SYMBOLS: List[str] = [
    "->", "==", "!=", "<=", ">=", "=>", "||", "${",
    "(", ")", "{", "}", "[", "]", ",", ":", ";", ".", "=", "<",
    "+", "-", "*", "/", "%",
]

TYPE_KW_GBNF = "(" + " | ".join(f'"{k}"' for k in TYPE_KEYWORDS) + ")"
TYPE_KW_SCALAR_GBNF = "(" + " | ".join(f'"{k}"' for k in TYPE_KEYWORDS if k not in ("decimal", "array")) + ")"

# lexer lexical shapes, verbatim in GBNF terminal form
INT_GBNF = '[0-9] ([0-9] | "_")* [0-9] | [0-9]'
FLOAT_GBNF = "[0-9] ([0-9] | \"_\")* \".\" [0-9] ([0-9] | \"_\")*"
STR_GBNF = r'"\"" ( [^"\\] | "\\" ( ["\\nt] ) )* "\""'

# ---------------------------------------------------------------------------
# rules: name -> (doc, [alternatives]); alternatives are plain GBNF bodies where
# bare identifiers are rule refs, double-quoted strings are terminals, and
# `::` is an ignorable separator token (stripped at emit time)
# ---------------------------------------------------------------------------
RULES: Dict[str, object] = {}

RULES["root"] = ("a .strata module: { top_decl }", ["top-decl*"])

RULES["top-decl"] = ("one top-level declaration", [
    "source-decl", "contract-decl", "model-decl", "pipeline-decl",
    "fn-decl", "import-decl", "test-decl", "generator-call",
])
RULES["generator-call"] = ("top-level model generator call (fn emitting models)", [
    r'ident "(" call-args? ")"',
])
RULES["test-decl"] = ("declarative data test on a model (expect row_count or expect <col> op literal)", [
    r'"test" ident "{" expect-list? "}"',
])
RULES["expect-list"] = ("one or more expect clauses; the parser treats ';' as an optional separator (no mandatory semicolon)", [
    r'(expect (";"? expect)* (";"?)? )?',
])
RULES["expect"] = ("a single expect clause", [
    r'"expect" expect-target comparison-op literal',
])
RULES["expect-target"] = ("which quantity to check: row_count or a column name", [
    r'"row_count" | ident',
])
RULES["comparison-op"] = ("comparison operators for test expectations", [
    r'"==" | "!=" | "<" | ">" | "<=" | ">="',
])

RULES["import-decl"] = ("import another .strata module", [
    r'"import" path-ref',
])

RULES["path-ref"] = ("dotted module path", [
    "ident (:: \".\" ident)*",
])

RULES["source-decl"] = ("external table declaration", [
    r'"source" ident "(" resource-arg-list? ")" ("{" source-prop-list "}")?',
])

RULES["resource-arg-list"] = ("source resource args: ident \":\" STR|IDENT, comma separated (no trailing comma)", [
    '(resource-arg (\",\" resource-arg)*)?',
])

RULES["resource-arg"] = ("ident \":\" STR|IDENT", [
    r'ident ":" (string | ident)',
])

RULES["named-arg-list"] = ("override/resource args: ident \":\" value, comma separated", [
    "(named-arg (\",\" named-arg)* \",\"?)?",
])

RULES["named-arg"] = ("ident \":\" STR|IDENT|INT", [
    r'ident ":" (string | ident | int-lit)',
])

RULES["source-prop-list"] = ("source properties (parser: optional single comma between, trailing comma ok)", [
    "(source-prop (\",\"? source-prop)* \",\"?)?",
])

RULES["source-prop"] = ("columns block | ident \":\" scalar value", [
    r'"columns" ":" "{" contract-field-list "}"',
    r'ident ":" (string | int-lit | ident)',
])

RULES["contract-field-list"] = ("fields; per field: optional one \",\" then optional one \";\" (parser form)", [
    "(contract-field \",\"? \";\"?)*",
])

RULES["contract-decl"] = ("typed output contract", [
    r'"contract" ident "{" contract-field-list "}"',
])

RULES["contract-field"] = ("name : type annotations", [
    r'ident ":" type-spec type-annot*',
])

RULES["type-spec"] = ("builtin type with optional parameters", [
    r'"decimal" "(" int-lit "," int-lit ")"',
    r'"array" "(" type-kw ")"',
    r'"money" ("(" ident ")")?',
    "type-kw-scalar",
])

RULES["type-kw-scalar"] = ("builtin type keyword sans decimal/array (must be parameterized)", [
    "TYPE_KW_SCALAR",
])

RULES["type-annot"] = ("column annotation (parser.accepts: nonnull/unique/primary_key/protected/enum/classification)", [
    r'"nonnull"', r'"unique"', r'"primary_key"', r'"protected"',
    r'"enum" "{" enum-value-list "}"',
    r'"classification" ":" (string | ident)',
])

RULES["enum-value-list"] = ("enum values: quoted strings or bare identifiers, comma separated", [
    "(enum-value (\",\" enum-value)* \",\"?)?",
])

RULES["enum-value"] = ("a single enum value", [
    "string", "ident",
])

RULES["model-decl"] = ("model: pure Table -> Table with optional contract", [
    r'"model" (ident | string) ("->" "contract" ident)? "{" model-item* "}"',
])

RULES["model-item"] = ("model attribute or statement", [
    r'"owner" ":" string',
    r'"reason" ":" string',
    r'"description" ":" string',
    r'"label" ":" string',
    "model-stmt",
])

RULES["model-stmt"] = ("relational statement", [
    "from-stmt", "join-stmt", "filter-stmt", "let-stmt",
    "select-stmt", "derive-stmt", "aggregate-stmt",
    "group-stmt", "sort-stmt", "take-stmt", "expand-stmt",
])

RULES["from-stmt"] = ("source table/model reference", [
    r'"from" ident',
])

RULES["join-stmt"] = ("join kinds with on-condition", [
    r'("join_left" | "join_inner" | "join_anti" | "join_semi") ident "on" expr',
])

RULES["filter-stmt"] = ("row filter (filter|where)", [
    r'("filter" | "where") expr',
])

RULES["let-stmt"] = ("hidden column", [
    r'"let" ident "=" expr',
])

RULES["select-stmt"] = ("output projection", [
    r'"select" "{" out-assign-list "}"',
])

RULES["derive-stmt"] = ("persisted column", [
    r'"derive" "{" out-assign-list "}"',
])

RULES["aggregate-stmt"] = ("aggregation", [
    r'"aggregate" "{" out-assign-list "}"',
])

RULES["out-assign-list"] = ("ident = expr assignments", [
    "(out-assign (\",\" out-assign)* \",\"?)?",
])

RULES["out-assign"] = ("ident = expr", [
    r'ident "=" expr',
])

RULES["group-stmt"] = ("group keys with nested body", [
    r'"group" "{" expr-list "}" "(" group-stmt-body* ")"',
])

RULES["group-stmt-body"] = ("filter|aggregate|sort|take inside group", [
    "filter-stmt", "aggregate-stmt", "sort-stmt", "take-stmt",
])

RULES["sort-stmt"] = ("ordering keys", [
    r'"sort" "{" sort-key-list "}"',
])

RULES["sort-key-list"] = ("keys, comma separated", [
    "(sort-key (\",\" sort-key)* \",\"?)?",
])

RULES["sort-key"] = ("expr with optional asc/desc (parser form)", [
    'expr ("desc" | "asc")?',
])

RULES["take-stmt"] = ("take N [.. M] (parser form)", [
    r'"take" int-lit (".." int-lit)?',
])

RULES["expand-stmt"] = ("one row per array element of a primary input column", [
    r'"expand" ident ("as" ident)?',
])

RULES["pipeline-decl"] = ("pipeline: models + source overrides", [
    r'"pipeline" ident ("env" ":" ident)? "{" pipeline-item-list "}"',
])

RULES["pipeline-item-list"] = ("pipeline items (comma separated, trailing comma allowed, may be empty)", [
    '(pipeline-item ("," pipeline-item)* ","?)?',
])

RULES["pipeline-item"] = ("pipeline attribute or entry (parser keys: env, models, sources)", [
    r'"env" ":" ident',
    r'"models" ":" "[" expr-list "]"',
    r'"sources" ":" "{" source-override-list "}"',
])

RULES["source-override-list"] = ("per-source from(...) overrides", [
    "(source-override (\",\" source-override)* \",\"?)?",
])

RULES["source-override"] = ("ident (optional override: `ident: from(kv)`)", [
    r'ident (":" "from" "(" named-arg-list? ")")?',
])

RULES["fn-decl"] = ("compile-time pure function (braces optional)", [
    r'"fn" ident "(" param-list? ")" "->" type-str "{" expr "}"',
    r'"fn" ident "(" param-list? ")" "->" type-str expr',
])

RULES["param-list"] = ("fn parameters (no trailing comma)", [
    'ident ":" type-str (\",\" ident ":" type-str)*',
])

RULES["type-str"] = ("List<...> | TYPE_KW | ident", [
    r'"List" "<" type-str ">"',
    "type-kw", "ident",
])

RULES["expr-list"] = ("expressions, comma separated", [
    "(expr (\",\" expr)* \",\"?)?",
])

RULES["string"] = ("double-quoted, \\n \\t \\\" \\\\ escapes, ${expr} slots", [
    "STR",
])

RULES["int-lit"] = ("integer literal (underscores allowed)", [
    "INT",
])

RULES["float-lit"] = ("float literal", [
    "FLOAT",
])

RULES["ident"] = ("identifier", [
    "IDENT",
])

RULES["type-kw"] = ("builtin type keyword", [
    "TYPE_KW",
])

RULES["literal"] = ("scalar literal", [
    "int-lit", "float-lit", "string", r'"true"', r'"false"', r'"null"',
])

RULES["column-ref"] = ("bare or schema-qualified column", [
    r'ident ("." ident)?',
])

RULES["kwarg"] = ("named call argument", [r'ident ":" expr'])
RULES["call-arg"] = ("positional or named argument", ["expr", "kwarg"])
RULES["call-args"] = ("comma-separated call arguments", [
    r'call-arg ("," call-arg)* ","?',
])
RULES["call-expr"] = ("built-in or user call", [
    r'ident "(" call-args? ")"',
    r'ident "(" call-args? ")" "over" "(" window-spec-list? ")"',
])

RULES["window-spec-list"] = ("over clauses, comma separated", [
    '(window-spec ("," window-spec)?)?',
])

RULES["window-spec"] = ("partition_by: [expr-list] | sort: [expr desc, ...]", [
    r'"partition_by" ":" "[" "]"',
    r'"partition_by" ":" "[" expr-list "]"',
    r'"sort" ":" "[" "]"',
    r'"sort" ":" "[" sort-key-list "]"',
])

RULES["unary"] = ("unary operators (parser: - and not)", [
    r'("-" | "not") unary', "postfix",
])

RULES["postfix"] = ("primary (call suffixes reserved for v0.2)", [
    "primary",
])

RULES["primary"] = ("literal | ref | call | parenthesized | list/comprehension | inline model", [
    "literal", "column-ref", "call-expr",
    r'"(" expr ")"',
    r'"[" "]"',
    r'"[" expr-list "]"',
    r'"[" expr "for" ident "in" expr "]"',
    "model-value",
])

RULES["model-value"] = ("inline model expression (fn bodies)", [
    r'"model" (ident | string) ("->" "contract" ident)? "{" model-item* "}"',
])

RULES["binop"] = ("binary operators (documentation; ops are inlined in the ladder)", [
    r'"or"', r'"and"', r'"=="', r'"!="', r'"<"', r'"<="', r'">"', r'">="',
    r'"+"', r'"-"', r'"*"', r'"/"', r'"%"', r'"||"', r'"in"',
])

# operator ladder for expr (low -> high), emitted as chained rules
PRECEDENCE: List[List[str]] = [
    ["or"],
    ["and"],
    ["==", "!=", "<", "<=", ">", ">=", "in"],
    ["+", "-", "||"],
    ["*", "/", "%"],
]

# lexer-reserved words with no production in the reference grammar yet
# (kept in lockstep with lexer.KEYWORDS by test_grammar)
RESERVED_UNUSED = {"all", "is", "=>", "freshness"}

LEXICAL = {
    "STR": STR_GBNF,
    "INT": INT_GBNF,
    "FLOAT": FLOAT_GBNF,
    "IDENT": "[a-zA-Z_] [a-zA-Z0-9_]*",
    "TYPE_KW": TYPE_KW_GBNF,
    "TYPE_KW_SCALAR": TYPE_KW_SCALAR_GBNF,
    "WS": r"[ \t\r\n]*",
}


def _ref(name: str) -> str:
    return name.replace("-", "_")


def _strip_seps(body: str) -> str:
    return " ".join(p for p in (s.strip() for s in body.split("::")) if p)


_QUOTED_SPLIT = re.compile(r'("[^"]*")')


def _norm_hyphens(body: str) -> str:
    """Replace hyphens ONLY in unquoted segments (they are rule-name connectors;
    quoted terminals like \"-\" must stay verbatim)."""
    out = []
    for i, seg in enumerate(_QUOTED_SPLIT.split(body)):
        out.append(seg if i % 2 else seg.replace("-", "_"))
    return "".join(out)


def _emit_alt(alt: str) -> str:
    if alt == "STR":
        return STR_GBNF
    if alt == "INT":
        return INT_GBNF
    if alt == "FLOAT":
        return FLOAT_GBNF
    if alt == "IDENT":
        return LEXICAL["IDENT"]
    if alt == "TYPE_KW":
        return TYPE_KW_GBNF
    if alt == "TYPE_KW_SCALAR":
        return TYPE_KW_SCALAR_GBNF
    return _norm_hyphens(_strip_seps(alt))


def emit_gbnf() -> str:
    """Emit the whole grammar as llama.cpp GBNF text (root = ::=_ root)."""
    lines: List[str] = []
    for op_idx, level in enumerate(PRECEDENCE):
        if op_idx < len(PRECEDENCE) - 1:
            nxt = f"expr_{op_idx + 1}"
            ops = " | ".join(f'"{o}"' for o in level)
            lines.append(f"expr_{op_idx} ::= {nxt} (({ops}) {nxt})*")
        else:
            ops = " | ".join(f'"{o}"' for o in level)
            lines.append(f"expr_{op_idx} ::= unary (({ops}) unary)*")
    lines.append("expr ::= expr_0")
    for name, (doc, alts) in RULES.items():
        if name in ("expr",):
            continue
        emitted = [_emit_alt(a) for a in alts]
        lines.append(f"{_ref(name)} ::= {' | '.join(emitted)}")
    for tname, shape in LEXICAL.items():
        if tname == "WS":
            continue
        if tname == "STR" or tname == "INT" or tname == "FLOAT" or tname == "IDENT" or tname == "TYPE_KW" or tname == "TYPE_KW_SCALAR":
            continue  # already inlined at use sites
        lines.append(f"{tname.lower()} ::= {shape}")
    lines.append(r'ws ::= [ \t\r\n]*')
    return "\n".join(lines) + "\n"


def validate() -> List[str]:
    """Fail-loud consistency checks; returns a list of problems (empty == ok)."""
    problems: List[str] = []
    for name, (_doc, alts) in RULES.items():
        if not alts:
            problems.append(f"rule {name}: no alternatives")
    joined = " ".join(
        alt for _doc, alts in RULES.values() for alt in alts
    )
    for kw in KEYWORDS:
        if kw in ("filter", "where"):
            continue
        if kw in RESERVED_UNUSED:
            continue
        if f'"{kw}"' not in joined:
            problems.append(f"keyword {kw!r} never used in any rule body")
    known = set(RULES) | {"expr", "unary", "postfix", "primary"}
    for _name, (_doc, alts) in RULES.items():
        for alt in alts:
            body = _strip_seps(alt) if alt not in LEXICAL else alt
            toks = (body.replace("(", " ").replace(")", " ")
                        .replace("|", " ").replace("*", " ").replace("?", " "))
            for tok in toks.split():
                if tok.startswith('"'):
                    continue
                if tok in known:
                    continue  # rule ref (a rule may share a name with a keyword)
                if tok.isupper() and tok in LEXICAL:
                    continue
                if tok in KEYWORDS or tok in TYPE_KEYWORDS:
                    problems.append(f"bare keyword {tok!r} used as rule ref in {alt!r}")
                    continue
                if not tok[0].islower():
                    problems.append(f"unknown token {tok!r} in {alt!r}")
                    continue
                if tok not in known and tok not in LEXICAL:
                    problems.append(f"unknown rule ref {tok!r} in {alt!r}")
    return problems

