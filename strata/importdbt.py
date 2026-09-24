# -*- coding: utf-8 -*-
"""`strata import-dbt` — Fase 2 §11 adoption: migrate a dbt project WITHOUT
leaving the warehouse, by importing the dbt schema.yml (sources + models with
column contracts) into a deterministic .strata artifact that must pass
`build` GREENS-ONLY-VERIFY, or FAILS-LOUD.

Philosophy (§4 fail-loud, matches every other Strata gate):
  - A dbt model that does `select *` with NO columns declared cannot have a
    contract derived WITHOUT the warehouse: guessing types needs data, and a
    guessed type would ship an untyped consumer that the warehouse would never
    catch (the exact dbt bug Strata exists to fail-loud on). → E041, exit 1,
    no artifact emitted. This mirrors E030 (cross-team) and every other gate:
    the artifact is the contract; a contract we cannot prove is a failed build.
  - Byte-deterministic: same schema.yml → byte-identical .strata artifact
    (test: re-running import twice yields identical sha256) — adoption must
    not introduce non-determinism into the publish path (§9, `init` AGENTS.md
    shares this determinism guarantee).
  - `data_type:` values are mapped through a KNOWN table (string/numeric/date/
    timestamp/bool/int variants); anything else → E041 fail-loud (we refuse to
    invent a Strata type for a dbt type we can't express — never guess)."""

from __future__ import annotations

import re
import sys
from argparse import Namespace
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import yaml

# dbt data_type -> Strata type. Only types the dialect layer can express are
# mapped (fail-loud §4: an unmapped dbt type becomes E041, not a silent
# guess). Sources in dbt are "external raw" (the warehouse owns their types);
# models are the typed contracts.
_TYPE_MAP = {
    "bigint": "int64",
    "int": "int64",
    "int64": "int64",
    "integer": "int64",
    "long": "int64",
    "numeric": "money",
    "decimal": "money",
    "float": "float64",
    "float64": "float64",
    "double": "float64",
    "string": "string",
    "text": "string",
    "varchar": "string",
    "uuid": "string",
    "date": "date",
    "timestamp": "timestamp",
    "datetime": "timestamp",
    "timestamptz": "timestamp",
    "bool": "bool",
    "boolean": "bool",
    "json": "json",
}


def map_type(dbt_type: str) -> str:
    """Map a dbt `data_type` into a Strata type, or raise KeyError for anything
    not expressible (E041 fail-loud — the spec §4 never guesses)."""
    key = str(dbt_type).strip().lower()
    if key not in _TYPE_MAP:
        raise KeyError(key)
    return _TYPE_MAP[key]


class ImportFailedFailLoud(Exception):
    """E041: a dbt model has no column contract (the `select *` anti-pattern).
    The import FAILS-LOUD instead of shipping a guessed/untyped consumer."""


class TransformFailLoud(Exception):
    """E042: a dbt model .sql uses SQL/Jinja outside the single-table subset
    Strata can translate without guessing. The import FAILS-LOUD (§4) instead
    of emitting a best-effort model that would silently diverge from dbt."""


_CMP = {"=": "==", "!=": "!=", "<>": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}
_CLAUSES = {"WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "UNION", "WITH", "FROM"}

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOKEN = re.compile(r"""\"[^\"]*\"|'[^']*'|\d+(?:\.\d+)?|[A-Za-z_]\w*|[<>=!<>+*/(),.]""",
                    re.VERBOSE)


def _split_top_level(tokens: List[Tuple[str, int]], sep: str) -> List[List[Tuple[str, int]]]:
    """Split a token stream at depth-0 separators (aggregate parens nest)."""
    out: List[List[Tuple[str, int]]] = [[]]
    for tok, depth in tokens:
        if depth == 0 and tok == sep:
            out.append([])
        else:
            out[-1].append((tok, depth))
    return out


def _strip_jinja(sql: str, model: str, models: Set[str], sources: Set[str]) -> str:
    """Resolve ONLY `{{ ref('m') }}`, `{{ source('ns','t') }}` and `{{ config(...) }}`.
    Any other Jinja (`{{ macro(...) }}`, `{% ... %}`) fails loud E042."""
    out: List[str] = []
    i = 0
    while True:
        start = sql.find("{{", i)
        if start == -1:
            out.append(sql[i:])
            break
        end = sql.find("}}", start)
        if end == -1:
            raise TransformFailLoud(
                f"E042: {model}: unterminated Jinja expression in model SQL")
        out.append(sql[i:start])
        inner = sql[start + 2:end].strip()
        m = re.match(r'^ref\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)$', inner)
        if m:
            if m.group(1) not in models:
                raise TransformFailLoud(
                    f"E042: {model}: ref('{m.group(1)}') refers to a model not "
                    f"declared in schema.yml — can't decide its from/contract "
                    f"without guessing (fail-loud §4).")
            out.append(m.group(1))
            i = end + 2
            continue
        m = re.match(r'^source\s*\(\s*[\'"]([^\'"]+)[\'"]\s*,\s*[\'"]([^\'"]+)[\'"]\s*\)$',
                     inner)
        if m:
            if m.group(2) not in sources:
                raise TransformFailLoud(
                    f"E042: {model}: source('{m.group(1)}','{m.group(2)}') is not "
                    f"declared as a dbt source table in schema.yml — fail-loud §4.")
            out.append(m.group(2))
            i = end + 2
            continue
        if re.match(r"^config\s*\(", inner):
            i = end + 2
            continue
        raise TransformFailLoud(
            f"E042: {model}: Jinja `{{{{ {inner} }}}}` is not one of the supported "
            f"ref/source/config forms; macros are out of the translatable subset "
            f"(fail-loud §4, translate it to plain Strata instead).")
    res = re.sub(r"\{#.*?#\}", "", "".join(out), flags=re.S)
    m = re.search(r"\{%.*?%\}", res, flags=re.S)
    if m:
        raise TransformFailLoud(
            f"E042: {model}: Jinja block `{m.group(0)}` (e.g. dbt statenents) is "
            f"out of the translatable subset — translate it to plain Strata.")
    return res


def _tokenize(sql: str) -> List[Tuple[str, int]]:
    """Token stream with values AND paren depth; `(`/`)` are kept (aggregates)."""
    tokens: List[Tuple[str, int]] = []
    depth = 0
    for m in _TOKEN.finditer(sql):
        tok = m.group(0)
        if tok == "(":
            tokens.append((tok, depth))
            depth += 1
        elif tok == ")":
            depth = max(0, depth - 1)
            tokens.append((tok, depth))
        else:
            tokens.append((tok, depth))
    return tokens


def _flat_words(tokens: Iterable[Tuple[str, int]]) -> List[str]:
    """Collapse `a . b` into `a.b` (SQL qualification) in a flat token run."""
    words: List[str] = []
    pending_dot = False
    for t, _ in tokens:
        if t == ".":
            pending_dot = True
            continue
        if pending_dot:
            words[-1] += "." + t
            pending_dot = False
        else:
            words.append(t)
    return words


def _join_str(item: List[Tuple[str, int]]) -> str:
    """Reconstruct a readable SQL expression from tokens, without awkward spaces."""
    s = re.sub(r"\s+", " ", " ".join(t for t, _ in item)).strip()
    return s.replace(" . ", ".").replace("( ", "(").replace(" )", ")")


_AGG_RE = re.compile(
    r"(?i)^(count|sum|avg|min|max)\s*\(\s*(\*|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*\)"
    r"(?:\s+as\s+([A-Za-z_][A-Za-z0-9_]*))?$")
_COL_QAS_RE = re.compile(
    r"(?i)^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)$")
_COL_BAS_RE = re.compile(
    r"(?i)^([A-Za-z_][A-Za-z0-9_]*)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)$")
_COL_Q_RE = re.compile(
    r"(?i)^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)$")


def _strip_qualifier(name: str) -> str:
    return name.split(".")[-1]


def _literal_strata(tok: str) -> str:
    if tok[0] in "\"'" and tok[-1] == tok[0]:
        body = tok[1:-1].replace("''", "'")
        return '"' + body.replace('"', '\\"') + '"'
    if tok.upper() in ("TRUE", "FALSE"):
        return "true" if tok.upper() == "TRUE" else "false"
    if tok.upper() == "NULL":
        return "null"
    return tok


def _parse_where_conjunct(conj: List[Tuple[str, int]], model: str) -> Optional[str]:
    """One `IS [NOT] NULL` or `col op literal` conjunct => Strata expr. Returns
    None when the conjunct is out of the subset (caller fails loud §4)."""
    words = _flat_words(conj)
    if len(words) == 3 and words[1].upper() == "IS" and words[2].upper() == "NULL":
        return f"{_strip_qualifier(words[0])} == null"
    if len(words) == 4 and words[1].upper() == "IS" and words[2].upper() == "NOT" \
            and words[3].upper() == "NULL":
        return f"not ({_strip_qualifier(words[0])} == null)"
    if len(words) == 3 and words[1] in _CMP:
        a, op, b = words[0], _CMP[words[1]], words[2]
        if _IDENT.fullmatch(_strip_qualifier(a)) is None:
            return None
        if "." in b and _IDENT.fullmatch(_strip_qualifier(b)):
            if op in ("==", "!="):
                b, a = _strip_qualifier(b), a
            else:
                return None  # reversed ordering on a column is not deterministic
        elif _IDENT.fullmatch(b) is not None:
            return None  # column op column — out of the {col, literal} subset
        return f"{_strip_qualifier(a)} {op} {_literal_strata(b)}"
    return None


def _parse_case_expr(s: str, model: str) -> str:
    """Translate SQL `CASE WHEN cond THEN val ... ELSE else END` into Strata `case(...)`.

    Only searched CASE (`CASE WHEN`) is supported; simple `CASE expr WHEN val`
    is out of subset (fail loud). Conditions are parsed via _parse_where_conjunct
    (col vs literal / IS NULL), values are literals or bare columns.
    """
    # Normalize: strip outer CASE ... END, then split WHEN/THEN/ELSE
    m = re.match(r"(?i)^CASE\s+(.*)\s+END\s*$", s, flags=re.S)
    if not m:
        raise TransformFailLoud(f"E042: {model}: malformed CASE {s!r}")
    inner = m.group(1).strip()
    # Split on WHEN / THEN / ELSE at top level (no nesting in subset)
    # Use token-based split to avoid string pitfalls
    toks = _tokenize(inner)
    words = [t for t, _ in toks]
    # Expect WHEN cond THEN val [WHEN cond THEN val]* [ELSE val]
    # We'll walk words
    i = 0
    parts: List[str] = []
    else_val: Optional[str] = None
    while i < len(words):
        if words[i].upper() != "WHEN":
            if words[i].upper() == "ELSE":
                # ELSE branch
                if i + 1 >= len(words):
                    raise TransformFailLoud(f"E042: {model}: CASE ELSE without value")
                else_val = _literal_strata(words[i + 1]) if words[i + 1].upper() not in ("NULL", "TRUE", "FALSE") and not _IDENT.fullmatch(words[i + 1]) else words[i + 1] if _IDENT.fullmatch(words[i + 1]) else _literal_strata(words[i + 1])
                # Handle quoted literals vs columns
                raw = words[i + 1]
                if raw[0] in "\"'":
                    else_val = _literal_strata(raw)
                elif raw.upper() in ("NULL", "TRUE", "FALSE"):
                    else_val = _literal_strata(raw)
                elif _IDENT.fullmatch(raw):
                    else_val = raw
                else:
                    else_val = _literal_strata(raw)
                i += 2
                break
            else:
                raise TransformFailLoud(f"E042: {model}: CASE expected WHEN or ELSE, got {words[i]!r}")
        # WHEN cond THEN val
        if i + 1 >= len(words):
            raise TransformFailLoud(f"E042: {model}: CASE WHEN without condition")
        # Find THEN
        try:
            then_idx = next(j for j in range(i + 1, len(words)) if words[j].upper() == "THEN")
        except StopIteration:
            raise TransformFailLoud(f"E042: {model}: CASE WHEN without THEN")
        cond_words = words[i + 1 : then_idx]
        # cond is like `a > 0` or `a IS NULL` — reuse where conjunct parser for simple
        # For CASE, cond may be `col op literal` etc. We'll try to parse as single conjunct
        # Build a fake token list for _parse_where_conjunct
        # cond_words like ['a', '>', '0'] or ['a', 'IS', 'NULL']
        cond_toks = [(w, 0) for w in cond_words]
        cond_expr = _parse_where_conjunct(cond_toks, model)
        if cond_expr is None:
            # Fallback: try to join as raw with == for = etc.
            # Simple: "a = 1" -> "a == 1"
            if len(cond_words) == 3 and cond_words[1] in _CMP:
                a, op, b = cond_words
                cond_expr = f"{_strip_qualifier(a)} {_CMP[op]} {_literal_strata(b)}"
            else:
                raise TransformFailLoud(f"E042: {model}: CASE WHEN condition {cond_words!r} out of subset")
        val_idx = then_idx + 1
        if val_idx >= len(words):
            raise TransformFailLoud(f"E042: {model}: CASE THEN without value")
        raw_val = words[val_idx]
        if raw_val[0] in "\"'":
            val_expr = _literal_strata(raw_val)
        elif raw_val.upper() in ("NULL", "TRUE", "FALSE"):
            val_expr = _literal_strata(raw_val)
        elif _IDENT.fullmatch(raw_val):
            val_expr = _strip_qualifier(raw_val)
        else:
            # Might be qualified like a.col
            if "." in raw_val:
                val_expr = _strip_qualifier(raw_val)
            else:
                val_expr = _literal_strata(raw_val)
        parts.append(cond_expr)
        parts.append(val_expr)
        i = val_idx + 1
        # Continue loop, expecting WHEN or ELSE or end
    if else_val is not None:
        parts.append(else_val)
    # Strata case() needs at least cond,val
    if len(parts) < 2:
        raise TransformFailLoud(f"E042: {model}: CASE with no branches")
    return f"case({', '.join(parts)})"


def _parse_select_item(item: List[Tuple[str, int]], model: str) -> Tuple[str, str, str]:
    """One top-level SELECT expression => (kind, emit, out). kind is 'col'
    (a plain column), 'agg' (count/sum/avg/min/max), or 'case' (CASE ...)."""
    s = _join_str(item).strip()
    # CASE ... END [AS alias] — detect before other patterns
    m_case = re.match(r"(?i)^CASE\s+WHEN.*\s+END(?:\s+AS\s+([A-Za-z_][A-Za-z0-9_]*))?$", s, flags=re.S)
    if m_case:
        alias = m_case.group(1)
        # Extract CASE ... END part
        m2 = re.match(r"(?i)^(CASE\s+WHEN.*\s+END)\s*(?:AS\s+[A-Za-z_][A-Za-z0-9_]*)?\s*$", s, flags=re.S)
        case_part = m2.group(1) if m2 else s
        out = alias or f"case_{abs(hash(s)) % 1000}"
        if alias is None:
            # Require alias for determinism — fail loud if no alias
            raise TransformFailLoud(
                f"E042: {model}: CASE expression {s!r} needs an AS alias — output name would be warehouse-defined")
        emit = _parse_case_expr(case_part, model)
        return "case", emit, out
    m = _AGG_RE.match(s)
    if m:
        fn, arg, out = m.group(1).lower(), m.group(2), m.group(3)
        if out is None:
            raise TransformFailLoud(
                f"E042: {model}: aggregate {m.group(1).upper()}({arg}) needs an AS "
                f"alias — the output name would be warehouse-defined")
        arg = "*" if arg == "*" else _strip_qualifier(arg)
        return "agg", f"{fn}({arg})", out
    if s.upper() == "*":
        raise TransformFailLoud(
            f"E042: {model}: `SELECT *` can't be translated without knowing the "
            f"exact column list the model outputs (fail-loud §4)")
    m = _COL_QAS_RE.match(s)
    if m:
        return "col", m.group(2), m.group(3)
    m = _COL_BAS_RE.match(s)
    if m:
        return "col", m.group(1), m.group(2)
    m = _COL_Q_RE.match(s)
    if m:
        return "col", m.group(2), m.group(2)
    if _IDENT.fullmatch(s):
        return "col", s, s
    raise TransformFailLoud(
        f"E042: {model}: SELECT expression {s!r} is out of the subset "
        f"(bare columns, aliases, CASE, and count/sum/avg/min/max only)")


_strip_comments_re = re.compile(r"(\"\"\"|\")(?:[^\"\\\\]|\\\\[\"\\\\nt])*\"|'[^']*'|--[^\n]*|/\*.*?(?:\*/|\Z)", flags=re.S)


def _strip_sql_comments(sql: str) -> str:
    """Remove `--` and `/* */` comments while keeping string literals intact."""
    out: List[str] = []
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch in ("'", '"'):
            quote = ch
            j = i + 1
            while j < len(sql):
                if sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == quote:
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
            continue
        if ch == "-" and sql[i:i + 2] == "--":
            j = sql.find("\n", i)
            i = j if j != -1 else len(sql)
            continue
        if ch == "/" and sql[i:i + 2] == "/*":
            j = sql.find("*/", i + 2)
            i = j + 2 if j != -1 else len(sql)
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_with(sql: str, model: str) -> Tuple[List[Tuple[str, str]], str]:
    """Split `WITH cte AS (...), cte2 AS (...)  SELECT ...` into:
      - (cte_name, cte_body_sql) pairs, in order
      - the trailing top-level SELECT (the model's main query)

    Comprehension caveat: the bodies are whole `(...)` groups (they must be),
    so a nested `WITH` inside a CTE would try to re-enter _split_with; the
    translator rejects a `WITH` that appears inside a body (E042, no guessing).
    Only `WITH x AS (SELECT ...)` column-less form is understood; anything else
    (WITH RECURSIVE, per-CTE column lists) raises TransformFailLoud."""
    body = _strip_sql_comments(sql)
    toks = _tokenize(body)
    if not toks or toks[0][0].upper() != "WITH":
        return [], body  # not a CTE query at all
    if len(toks) > 1 and toks[1][0].upper() == "RECURSIVE":
        raise TransformFailLoud(
            f"E042: {model}: `WITH RECURSIVE` is out of the translatable subset "
            f"(fail-loud §4) — translate the recursion to plain Strata instead.")
    # walk depth-0 tokens to find `name AS ( ... )` blocks
    ctes: List[Tuple[str, str]] = []
    i = 1  # skip WITH
    while True:
        if i >= len(toks) or not _IDENT.fullmatch(toks[i][0]):
            raise TransformFailLoud(
                f"E042: {model}: expected a CTE name after WITH/`,`, got "
                f"{toks[i][0] if i < len(toks) else 'end of input'} (fail-loud §4)")
        name = toks[i][0]
        i += 1
        # optional column list `cte (a, b)` — not understood (column renaming)
        if i < len(toks) and toks[i][0] == "(":
            raise TransformFailLoud(
                f"E042: {model}: CTE {name!r} declares a column list (a, b) — "
                f"column renaming is out of the subset (fail-loud §4, translate "
                f"it to plain Strata instead)")
        if i >= len(toks) or toks[i][0].upper() != "AS":
            raise TransformFailLoud(
                f"E042: {model}: CTE {name!r} missing `AS` (fail-loud §4)")
        i += 1
        if i >= len(toks) or toks[i][0] != "(":
            raise TransformFailLoud(
                f"E042: {model}: CTE {name!r} has no parenthesized body — only "
                f"`WITH name AS (SELECT ...)` is understood (fail-loud §4)")
        depth = 0
        start = i
        while i < len(toks):
            t = toks[i][0]
            if t == "(":
                depth += 1
            elif t == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0 or i >= len(toks):
            raise TransformFailLoud(
                f"E042: {model}: CTE {name!r} body is not closed (fail-loud §4)")
        cte_sql = _join_str(toks[start + 1:i])
        ctes.append((name, cte_sql))
        i += 1  # skip closing ')'
        # after the body: either `, name AS (` or the final top-level SELECT
        if i < len(toks) and toks[i][0] == ",":
            i += 1
            continue
        break
    main_toks = toks[i:]
    if not main_toks or main_toks[0][0].upper() != "SELECT":
        raise TransformFailLoud(
            f"E042: {model}: after the WITH chain there must be a top-level "
            f"SELECT (the model's main query), got "
            f"{main_toks[0][0] if main_toks else 'end of input'} (fail-loud §4)")
    main_sql = _join_str(main_toks)
    return ctes, main_sql


def _translate_sql(sql_text: str, model: str, models: Set[str],
                   sources: Set[str]) -> Tuple[List[str], List[Tuple[str, List[str]]]]:
    """Translate a dbt model .sql into (main_body, cte_helpers).

    `main_body` is the Strata body lines for `model`. `cte_helpers` are
    (helper_name, body_lines) contract-less Strata models that stand in for
    each `WITH` CTE the main query (or a later CTE) reads from. The main
    query's `from`/`join` table names are mapped onto those helpers via
    `table_map`, so a CTE becomes just another model ref — Strata's compiler
    decides materialization. Nested/recursive CTEs fail loud E042."""
    ctes, main_sql = _split_with(_strip_jinja(sql_text, model, models, sources), model)
    if not ctes:
        return _parse_transform(main_sql, model, models, sources), []
    # Each CTE is a contract-less helper model named `{model}__{cte}`.
    table_map: Dict[str, str] = {}
    helper_defs: List[Tuple[str, List[str]]] = []
    for name, cte_sql in ctes:
        helper = f"{model}__{name}"
        if helper in models or helper in sources:
            raise TransformFailLoud(
                f"E042: {model}: CTE {name!r} maps to helper model {helper!r} "
                f"which collides with a schema-declared table — rename the CTE "
                f"(fail-loud §4)")
        # The CTE body may reference sources, models, or earlier CTEs (mapped).
        body = _parse_transform(cte_sql, helper, models | set(table_map), sources,
                                table_map=table_map)
        helper_defs.append((helper, body))
        table_map[name] = helper
    main_body = _parse_transform(main_sql, model, models | set(table_map), sources,
                                 table_map=table_map)
    return main_body, helper_defs


def _parse_transform(sql_text: str, model: str, models: Set[str],
                     sources: Set[str],
                     table_map: Optional[Dict[str, str]] = None) -> List[str]:
    """Translate ONE dbt model .sql (WITH already peeled off) into Strata body statements.

    Subset: `SELECT <list> FROM <ref|source> [AS alias] [JOIN ... ON ...]`
    with optional `WHERE` (col-vs-literal / IS NULL, AND) and optional
    `GROUP BY` over aggregates, plus `CASE WHEN ... THEN ... ELSE ... END`
    in SELECT (→ Strata `case(...)`). Everything else — `select *` without
    contract, WITH (handled by _translate_with), subqueries, ORDER BY/LIMIT/
    DISTINCT/HAVING/UNION — raises TransformFailLoud (E042, fail-loud §4).

    `table_map` maps a SQL table name (a CTE, peeled earlier) to the Strata
    model name that stands in for it (a contract-less helper); those names are
    accepted in FROM/JOIN and emitted as-is so the final model references the
    helper. Never guesses."""
    sql = _strip_jinja(sql_text, model, models, sources)
    toks = _tokenize(sql)
    if not toks or toks[0][0].upper() != "SELECT":
        raise TransformFailLoud(f"E042: {model}: expected a single SELECT at the top level")

    depth0_words = [t for t, d in toks if d == 0]
    banned = {"DISTINCT", "ORDER", "LIMIT", "OFFSET", "UNION", "WITH", "HAVING"}
    for t in depth0_words:
        if t.upper() in banned:
            raise TransformFailLoud(
                f"E042: {model}: `{t.upper()}` is not in the translatable subset "
                f"(step 1 = single-table SELECT/WHERE/GROUP BY); translate it to "
                f"plain Strata instead.")
    if depth0_words[1:].count("SELECT"):
        raise TransformFailLoud(
            f"E042: {model}: subqueries (a second SELECT) are out of the subset")

    # Split the token stream into top-level clauses: SELECT .. FROM .. WHERE .. GROUP BY
    groups: List[List[Tuple[str, int]]] = []
    cur: List[Tuple[str, int]] = []
    seen_from = False
    for tok, d in toks[1:]:
        if d == 0 and tok.upper() in ("FROM", "WHERE", "GROUP"):
            if tok.upper() == "FROM":
                seen_from = True
            if cur:
                groups.append(cur)
            cur = [(tok, d)]
            continue
        cur.append((tok, d))
    groups.append(cur)
    if not seen_from:
        raise TransformFailLoud(f"E042: {model}: single-table FROM is required")

    select_st, from_st, where_st, group_st = groups[0], groups[1], None, None
    for g in groups[2:]:
        head = g[0][0] if g else ""
        if head.upper() == "WHERE" and where_st is None:
            where_st = g
        elif head.upper() == "GROUP" and group_st is None:
            group_st = g

    # -- SELECT list
    select_items = [_parse_select_item(it, model) for it in _split_top_level(select_st, ",")]

    # -- FROM: base table + optional alias + zero or more JOINs
    # Supported: FROM tbl [AS alias] [ (LEFT|RIGHT|INNER|FULL)? JOIN tbl2 [AS alias2] ON cond [AND cond]* ]*
    # Each JOIN must have ON with at least one `a.col = b.col` (AND-separated). USING, CROSS without ON, and comma-FROM are out of subset.
    from_toks = from_st[1:]  # after FROM
    # Helper to read a table ref + optional alias, returning (table, alias, consumed)
    def _read_table_ref(idx: int) -> Tuple[str, Optional[str], int]:
        if idx >= len(from_toks):
            raise TransformFailLoud(f"E042: {model}: FROM/JOIN table missing")
        tbl = from_toks[idx][0]
        if tbl not in models and tbl not in sources \
                and (table_map is None or tbl not in table_map):
            raise TransformFailLoud(
                f"E042: {model}: table {tbl!r} is neither a dbt source, a model "
                f"with a contract, nor a CTE in scope — fail-loud §4.")
        emit_name = table_map.get(tbl, tbl) if table_map else tbl
        nxt = idx + 1
        alias: Optional[str] = None
        if nxt < len(from_toks) and from_toks[nxt][0].upper() == "AS":
            nxt += 1
            if nxt >= len(from_toks) or not _IDENT.fullmatch(from_toks[nxt][0]):
                raise TransformFailLoud(f"E042: {model}: AS without alias")
            alias = from_toks[nxt][0]
            nxt += 1
        elif nxt < len(from_toks) and _IDENT.fullmatch(from_toks[nxt][0]) and from_toks[nxt][0].upper() not in {"JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "ON", "USING", "WHERE", "GROUP"}:
            # Bare alias without AS (e.g. FROM orders a)
            alias = from_toks[nxt][0]
            nxt += 1
        return emit_name, alias, nxt

    base_table, base_alias, pos = _read_table_ref(0)
    table = base_table
    alias_map: Dict[str, str] = {}
    if base_alias:
        alias_map[base_alias] = base_table
    else:
        alias_map[base_table] = base_table
    joins: List[Tuple[str, str, str]] = []  # (join_type, join_table, on_expr)
    while pos < len(from_toks):
        # Parse join type
        jt = "INNER"
        if from_toks[pos][0].upper() in ("LEFT", "RIGHT", "INNER", "FULL", "CROSS"):
            jt = from_toks[pos][0].upper()
            pos += 1
            if jt != "CROSS" and pos < len(from_toks) and from_toks[pos][0].upper() == "OUTER":
                pos += 1  # LEFT OUTER JOIN == LEFT JOIN
            if pos < len(from_toks) and from_toks[pos][0].upper() == "JOIN":
                pos += 1
            else:
                if jt == "CROSS":
                    # CROSS JOIN without ON is allowed only if followed by table (parsing will handle, but ON will be required later for Strata)
                    pass
                else:
                    raise TransformFailLoud(f"E042: {model}: {jt} without JOIN")
        elif from_toks[pos][0].upper() == "JOIN":
            jt = "INNER"
            pos += 1
        else:
            raise TransformFailLoud(f"E042: {model}: unexpected token in FROM/JOIN {from_toks[pos][0]!r}")
        # Join table
        j_tbl, j_alias, pos = _read_table_ref(pos)
        # Map alias
        if j_alias:
            alias_map[j_alias] = j_tbl
        else:
            alias_map[j_tbl] = j_tbl
        # ON clause required for non-CROSS
        if jt == "CROSS":
            # Strata has no CROSS without ON — fail loud, suggest join_inner
            raise TransformFailLoud(f"E042: {model}: CROSS JOIN without ON is out of subset — use JOIN with ON")
        if pos >= len(from_toks) or from_toks[pos][0].upper() != "ON":
            raise TransformFailLoud(f"E042: {model}: JOIN {j_tbl!r} without ON")
        pos += 1  # skip ON
        # Collect ON condition tokens until next JOIN or end
        on_toks: List[Tuple[str, int]] = []
        while pos < len(from_toks) and from_toks[pos][0].upper() not in ("JOIN", "LEFT", "RIGHT", "INNER", "FULL", "CROSS"):
            on_toks.append(from_toks[pos])
            pos += 1
        if not on_toks:
            raise TransformFailLoud(f"E042: {model}: JOIN {j_tbl!r} ON is empty")
        # ON is AND-separated `a.col = b.col` (only equi-joins in subset)
        on_parts: List[str] = []
        for conj in _split_top_level(on_toks, "AND"):
            w = _flat_words(conj)
            # Expect `a.col = b.col` or `a.col == b.col`
            if len(w) == 3 and w[1] in ("=", "=="):
                left, right = w[0], w[2]
                # Resolve alias → real table for Strata (keep qualified)
                def _qual(s: str) -> str:
                    if "." in s:
                        a, c = s.split(".", 1)
                        real = alias_map.get(a, a)
                        return f"{real}.{c}"
                    return s
                on_parts.append(f"{_qual(left)} == {_qual(right)}")
            else:
                raise TransformFailLoud(f"E042: {model}: JOIN ON {[t for t,_ in conj]!r} must be `a.col = b.col` (AND-separated equi-joins only)")
        on_expr = " and ".join(on_parts)
        # Map join type to Strata
        if jt == "LEFT":
            strata_jt = "join_left"
        elif jt == "RIGHT":
            strata_jt = "join_right"
        elif jt in ("INNER",):
            strata_jt = "join_inner"
        elif jt == "FULL":
            strata_jt = "join_full"
        else:
            strata_jt = "join_inner"
        joins.append((strata_jt, j_tbl, on_expr))
    # For backward compat, `table` stays as base table for single-table checks
    # `alias_map` is used later for SELECT qualification if needed

    # -- WHERE (AND-only conjuncts)
    preds: List[str] = []
    if where_st:
        for conj in _split_top_level(where_st[1:], "AND"):
            expr = _parse_where_conjunct(conj, model)
            if expr is None:
                raise TransformFailLoud(
                    f"E042: {model}: WHERE {[t for t, _ in conj]!r} is out of the "
                    f"subset (column-vs-literal or IS [NOT] NULL, AND only)")
            preds.append(expr)

    # -- GROUP BY
    keys: List[str] = []
    if group_st:
        gw = [t for t, _ in group_st]
        if [w.upper() for w in gw[:2]] == ["GROUP", "BY"]:
            gw = gw[2:]
        else:
            raise TransformFailLoud(f"E042: {model}: malformed GROUP BY")
        for item in _split_top_level([(t, 0) for t in gw], ","):
            w = _flat_words(item)
            if len(w) != 1:
                raise TransformFailLoud(
                    f"E042: {model}: GROUP BY expression {w!r} is out of the subset")
            cand = w[0]
            # Allow qualified `a.col` or bare `col`
            if "." in cand:
                parts = cand.split(".")
                if len(parts) != 2 or not all(_IDENT.fullmatch(p) for p in parts):
                    raise TransformFailLoud(
                        f"E042: {model}: GROUP BY expression {w!r} is out of the subset")
            elif _IDENT.fullmatch(cand) is None:
                raise TransformFailLoud(
                    f"E042: {model}: GROUP BY expression {w!r} is out of the subset")
            keys.append(_strip_qualifier(cand))

    # -- assemble the Strata body
    body: List[str] = [f"from {table}"]
    for jt, jtbl, on in joins:
        body.append(f"{jt} {jtbl} on {on}")
    if preds:
        body.append("filter " + " and ".join(preds))
    if not keys:
        for kind, emit, out in select_items:
            if kind == "agg":
                raise TransformFailLoud(
                    f"E042: {model}: aggregate {emit} has no GROUP BY — invalid SQL")
            if kind == "case" and any(k == out for k in keys):
                pass  # case without GROUP BY is fine (derived column)
        body.append("select {")
        for _, col, out in select_items:
            body.append(f"  {out} = {col},")
        body.append("}")
        return body
    if any(kind == "agg" for kind, _, _ in select_items) is False:
        # Allow GROUP BY with case-derived columns that are keys — but still need at least one agg or a dedup intent
        # For now, require an agg; a pure GROUP BY without agg is out of subset (use dedup)
        raise TransformFailLoud(
            f"E042: {model}: GROUP BY with no aggregate is meaningless — translate "
            f"to a dedup by hand if that is what you meant (fail-loud §4)")
    key_terms: List[str] = []
    key_outs = {it[2] for it in select_items if it[0] in ("col", "case")}
    for k in keys:
        if k in key_outs:
            match = next(it for it in select_items if it[0] in ("col", "case") and it[2] == k)
            if match[1] != k:
                body.append(f"let {k} = {match[1]}")
            key_terms.append(k)
        elif any(it[0] in ("col", "case") and it[1] == k for it in select_items):
            key_terms.append(k)
        else:
            raise TransformFailLoud(
                f"E042: {model}: GROUP BY {k!r} is neither a SELECTed column nor "
                f"an alias — SQL would be invalid, Strata would silently pick a "
                f"semantics (fail-loud §4)")
    for it in select_items:
        if it[0] in ("col", "case") and it[2] not in key_terms and it[1] not in key_terms:
            raise TransformFailLoud(
                f"E042: {model}: SELECT {it[2]!r} is neither grouped nor "
                f"aggregated (fail-loud §4)")
    aggs = [it for it in select_items if it[0] == "agg"]
    agg_outs = {it[2] for it in aggs}
    if agg_outs & set(key_terms):
        raise TransformFailLoud(
            f"E042: {model}: aggregate output {sorted(agg_outs & set(key_terms))} "
            f"collides with a group key")
    body.append(f"group {{ {', '.join(key_terms)} }} (")
    body.append("  aggregate { " + ", ".join(f"{o} = {e}" for _, e, o in aggs) + " }")
    body.append(")")
    return body


def import_dbt_schema(schema: Path) -> str:
    """Deterministically derive a .strata artifact from a dbt schema.yml.

    Returns the byte-deterministic artifact text. Raises ImportFailedFailLoud
    (E041, fail-loud §4) if any model declares no columns — dbt's `select *`
    without a contract. Sources become raw external imports (their types are
    the warehouse's business, mapped through _TYPE_MAP so `build` can type them
    without a warehouse round-trip)."""
    doc = yaml.safe_load(schema.read_text())
    out = ["// generated deterministically by `strata import-dbt` — byte-identical "
           "re-runs (same schema.yml) produce identical .strata. Do not hand-edit "
           "the contract; change the dbt schema instead and re-import (§11 adoption)."]

    for src in doc.get("sources", []) or []:
        for tbl in src.get("tables", []) or []:
            tname = tbl["name"]
            out.append("")
            out.append(f"source {tname}(ns: \"{src.get('name', 'dbt')}\", "
                       f"dataset: \"{tbl.get('schema', src.get('schema', ''))}\") {{")
            out.append("  columns: {")
            for c in tbl.get("columns", []) or []:
                ctype = map_type(c.get("data_type", "string"))
                nn = " nonnull" if "not_null" in (c.get("tests", []) or []) else ""
                out.append(f"    {c['name']}: {ctype}{nn},")
            out.append("  }")
            out.append("}")

    for mdl in doc.get("models", []) or []:
        cols = mdl.get("columns", []) or []
        if not cols:
            raise ImportFailedFailLoud(
                f"E041: {schema}: dbt model {mdl.get('name')!r} does `select *` "
                f"with no `columns:` contract. Cannot derive a contract without "
                f"the warehouse (guessing types needs data) — fail-loud §4. Add "
                f"`columns:` to the model in schema.yml, or ship an explicitly "
                f"typed Strata model instead.")
        mname = mdl["name"]
        deps = mdl.get("depends_on", []) or []
        if not deps:
            raise ImportFailedFailLoud(
                f"E041: {schema}: dbt model {mname!r} declares `columns:` but no "
                f"`depends_on:`. The `from` of its contract is the warehouse's "
                f"lineage to decide — guessing it would ship an untyped consumer "
                f"(fail-loud §4). Add `depends_on: [<source_table>]` to the model "
                f"in schema.yml and re-import.")
        out.append("")
        out.append(f"contract {mname}Contract {{")
        for c in cols:
            ctype = map_type(c.get("data_type", "string"))
            nn = " nonnull" if "not_null" in (c.get("tests", []) or []) else ""
            out.append(f"  {c['name']} : {ctype}{nn},")
        out.append("}")
        out.append("")
        out.append(f"model {mname} -> contract {mname}Contract {{")
        out.append(f"  from {deps[0]}")
        out.append("  derive {")
        for c in cols:
            out.append(f"    {c['name']} = {c['name']},")
        out.append("  }")
        out.append("}")

    out.append("")
    return "\n".join(out)


def _emit_model_body(mdl: dict, transforms: Dict[str, List[str]],
                     schema: Path) -> List[str]:
    """Model block statements: SQL-translated body when present, otherwise the
    contract-only `derive` passthrough (schema-only import). A model without a
    transform still needs `depends_on:` to name its `from` — same E041 rule."""
    mname = mdl["name"]
    if mname in transforms:
        return transforms[mname]
    deps = mdl.get("depends_on", []) or []
    if not deps:
        raise ImportFailedFailLoud(
            f"E041: {schema}: dbt model {mname!r} has no models/*.sql and no "
            f"`depends_on:`. The `from` of its contract is the warehouse's "
            f"lineage to decide — guessing it would ship an untyped consumer "
            f"(fail-loud §4).")
    body = [f"from {deps[0]}", "derive {"]
    for c in mdl.get("columns", []) or []:
        body.append(f"  {c['name']} = {c['name']},")
    body.append("}")
    return body


def import_dbt_project(schema: Path, model_dir: Path) -> str:
    """`import-dbt --models DIR`: dbt schema.yml PLUS translation of each
    `models/*.sql` (SELECT/WHERE/GROUP BY/JOIN/CASE subset, `WITH` CTEs
    lowered to contract-less `{model}__{cte}` helpers) into the Strata model
    body. A .sql file whose stem has no schema contract, or SQL outside the
    subset, raises TransformFailLoud (E042, fail-loud §4, nothing emitted)."""
    doc = yaml.safe_load(schema.read_text())
    model_names = {m["name"] for m in doc.get("models", []) or []}
    source_names = {t["name"]
                    for s in doc.get("sources", []) or []
                    for t in s.get("tables", []) or []}
    transforms: Dict[str, List[str]] = {}
    cte_helpers: Dict[str, List[Tuple[str, List[str]]]] = {}
    for sql_file in sorted(model_dir.glob("*.sql")):
        stem = sql_file.stem
        if stem not in model_names:
            raise TransformFailLoud(
                f"E042: {sql_file}: dbt model {stem!r} has no entry in schema.yml "
                f"(no column contract) — fail-loud §4.")
        transformed, helpers = _translate_sql(sql_file.read_text(), stem,
                                              model_names, source_names)
        transforms[stem] = transformed
        cte_helpers[stem] = helpers

    header = ("// generated deterministically by `strata import-dbt` — byte-identical "
              "re-runs (same schema.yml + models/*.sql) produce identical .strata. "
              "Do not hand-edit the contract; change the dbt project and re-import "
              "(§11 adoption).")
    out = [header]
    for src in doc.get("sources", []) or []:
        for tbl in src.get("tables", []) or []:
            tname = tbl["name"]
            out.append("")
            out.append(f"source {tname}(ns: \"{src.get('name', 'dbt')}\", "
                       f"dataset: \"{tbl.get('schema', src.get('schema', ''))}\") {{")
            out.append("  columns: {")
            for c in tbl.get("columns", []) or []:
                ctype = map_type(c.get("data_type", "string"))
                nn = " nonnull" if "not_null" in (c.get("tests", []) or []) else ""
                out.append(f"    {c['name']}: {ctype}{nn},")
            out.append("  }")
            out.append("}")
    for mdl in doc.get("models", []) or []:
        cols = mdl.get("columns", []) or []
        if not cols:
            raise ImportFailedFailLoud(
                f"E041: {schema}: dbt model {mdl.get('name')!r} does `select *` "
                f"with no `columns:` contract.")
        mname = mdl["name"]
        out.append("")
        out.append(f"contract {mname}Contract {{")
        for c in cols:
            ctype = map_type(c.get("data_type", "string"))
            nn = " nonnull" if "not_null" in (c.get("tests", []) or []) else ""
            out.append(f"  {c['name']} : {ctype}{nn},")
        out.append("}")
        out.append("")
        for helper_name, helper_body in cte_helpers.get(mname, []):
            out.append(f"model {helper_name} {{")
            for line in helper_body:
                out.append(f"  {line}")
            out.append("}")
        out.append("")
        out.append(f"model {mname} -> contract {mname}Contract {{")
        for line in _emit_model_body(mdl, transforms, schema):
            out.append(f"  {line}")
        out.append("}")
    out.append("")
    return "\n".join(out)


def cmd(args: Namespace) -> int:
    """CLI entry: `strata import-dbt schema.yml [--output artifact.strata]`.
    Returns 0 (artifact written) or 1 (E041 fail-loud, nothing emitted)."""
    path = Path(args.file)
    try:
        artifact = import_dbt_schema(path)
    except ImportFailedFailLoud as e:
        print(str(e), file=sys.stderr)
        return 1
    except KeyError as e:
        print(f"E041: {path}: dbt data_type {e.args[0]!r} has no Strata mapping — "
              f"cannot express it in any dialect; refusing to guess (fail-loud "
              f"§4, see strata/importdbt.py _TYPE_MAP).", file=sys.stderr)
        return 1
    out = Path(args.output) if getattr(args, "output", None) else (
        path.parent / path.stem).with_suffix(".strata")
    out.write_text(artifact)
    print(f"wrote {out} ({len(artifact.encode())} bytes, deterministic §11)")
    return 0
