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


def _parse_select_item(item: List[Tuple[str, int]], model: str) -> Tuple[str, str, str]:
    """One top-level SELECT expression => (kind, emit, out). kind is 'col'
    (a plain column) or 'agg' (count/sum/avg/min/max)."""
    s = _join_str(item).strip()
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
        f"(bare columns, aliases, and count/sum/avg/min/max only)")


def _parse_transform(sql_text: str, model: str, models: Set[str],
                     sources: Set[str]) -> List[str]:
    """Translate ONE dbt model .sql (single table) into Strata body statements.

    Step-1 subset: `SELECT <list> FROM <ref|source> [AS alias]` with optional
    `WHERE` (column-vs-literal / IS [NOT] NULL, AND only) and optional `GROUP BY
    <cols>` over `count/sum/avg/min/max`. Everything else — `select *`, joins,
    CTEs, macros, ORDER BY, LIMIT, DISTINCT, expressions — raises
    TransformFailLoud (E042, fail-loud §4). Never guesses."""
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

    # -- FROM: exactly one table token (plus an optional single alias). A
    # multi-table FROM (`t1, t2`), a `JOIN ... ON`, or any dangling clause
    # FAILS LOUD instead of silently dropping half the lineage.
    from_words = [t for t, _ in from_st][1:]  # drop FROM keyword
    if not from_words:
        raise TransformFailLoud(f"E042: {model}: FROM is empty")
    table = from_words[0]
    if table not in models and table not in sources:
        raise TransformFailLoud(
            f"E042: {model}: FROM {table!r} is neither a dbt source table nor a "
            f"model with a schema.yml contract — fail-loud §4.")
    rest = from_words[1:]
    if rest and rest[0].upper() == "AS":
        rest = rest[1:]
    join_words = {"JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "ON", "USING"}
    if rest and (len(rest) != 1 or not _IDENT.fullmatch(rest[0])
                 or rest[0].upper() in join_words):
        if any(w.upper() in join_words for w in from_words[1:]):
            raise TransformFailLoud(
                f"E042: {model}: JOIN is out of the step-1 subset — translate the "
                f"join to a Strata `join_*` statement by hand (fail-loud §4)")
        raise TransformFailLoud(
            f"E042: {model}: FROM {table!r} has a trailing clause {rest!r} that "
            f"is not a single alias — fail-loud §4 (never guess)")

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
            if len(w) != 1 or _IDENT.fullmatch(w[0]) is None:
                raise TransformFailLoud(
                    f"E042: {model}: GROUP BY expression {w!r} is out of the subset")
            keys.append(_strip_qualifier(w[0]))

    # -- assemble the Strata body
    body: List[str] = [f"from {table}"]
    if preds:
        body.append("filter " + " and ".join(preds))
    if not keys:
        for kind, emit, out in select_items:
            if kind == "agg":
                raise TransformFailLoud(
                    f"E042: {model}: aggregate {emit} has no GROUP BY — invalid SQL")
        body.append("select {")
        for _, col, out in select_items:
            body.append(f"  {out} = {col},")
        body.append("}")
        return body
    if any(kind == "agg" for kind, _, _ in select_items) is False:
        raise TransformFailLoud(
            f"E042: {model}: GROUP BY with no aggregate is meaningless — translate "
            f"to a dedup by hand if that is what you meant (fail-loud §4)")
    key_terms: List[str] = []
    key_outs = {it[2] for it in select_items if it[0] == "col"}
    for k in keys:
        if k in key_outs:
            match = next(it for it in select_items if it[0] == "col" and it[2] == k)
            if match[1] != k:
                body.append(f"let {k} = {match[1]}")
            key_terms.append(k)
        elif any(it[0] == "col" and it[1] == k for it in select_items):
            key_terms.append(k)
        else:
            raise TransformFailLoud(
                f"E042: {model}: GROUP BY {k!r} is neither a SELECTed column nor "
                f"an alias — SQL would be invalid, Strata would silently pick a "
                f"semantics (fail-loud §4)")
    for it in select_items:
        if it[0] == "col" and it[2] not in key_terms and it[1] not in key_terms:
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
    `models/*.sql` (single-table subset) into the Strata model body. A .sql file
    whose stem has no schema contract, or SQL outside the subset, raises
    TransformFailLoud (E042, fail-loud §4, nothing emitted)."""
    doc = yaml.safe_load(schema.read_text())
    model_names = {m["name"] for m in doc.get("models", []) or []}
    source_names = {t["name"]
                    for s in doc.get("sources", []) or []
                    for t in s.get("tables", []) or []}
    transforms: Dict[str, List[str]] = {}
    for sql_file in sorted(model_dir.glob("*.sql")):
        stem = sql_file.stem
        if stem not in model_names:
            raise TransformFailLoud(
                f"E042: {sql_file}: dbt model {stem!r} has no entry in schema.yml "
                f"(no column contract) — fail-loud §4.")
        transforms[stem] = _parse_transform(sql_file.read_text(), stem,
                                            model_names, source_names)

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
