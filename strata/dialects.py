"""Dialect adapters: identifier quoting + type maps per target warehouse.

The main job of the adapter is not "prettier SQL" — it is the Strata guarantee
that "the same typed DAG compiles and runs on any warehouse you pin to."
Where a dialect cannot express something the plan declares (e.g. Snowflake
and BigQuery have no ANTI/SEMI JOIN keyword), the translator must fail loudly
instead of emitting a silently-wrong query.
"""
from __future__ import annotations

from typing import Dict, Optional


from .types import StrataType


class Dialect:
    def __init__(self, name: str, quote_ident, type_map: Dict[str, str],
                 decimal, money: str, array, supports_anti_semi: bool,
                 function_map: Optional[Dict[str, str]] = None):
        self.name = name
        self._quote = quote_ident
        self.type_map = type_map
        self._decimal = decimal
        self._array = array
        self.money = money
        self.supports_anti_semi = supports_anti_semi
        self.function_map = function_map or {}

    # -- identifiers --------------------------------------------------
    def ident(self, name: str) -> str:
        return self._quote(name)

    def qualified(self, alias: str, name: str) -> str:
        return f"{alias}.{self.ident(name)}"

    # -- types ----------------------------------------------------------
    def sql_type(self, name: str) -> Optional[str]:
        return self.type_map.get(name)

    def decimal_sql(self, p: int, s: int) -> str:
        return self._decimal(p, s)

    def array_sql(self, elem_sql: str) -> str:
        return self._array(elem_sql)

    def cast_target(self, spec: str) -> str:
        if spec in self.type_map:
            return self.type_map[spec]
        if spec == "money":
            return self.money
        if spec.startswith("decimal(") and spec.endswith(")"):
            inner = spec[len("decimal("):-1]
            p, s = (int(x.strip()) for x in inner.split(","))
            return self.decimal_sql(p, s)
        if spec == "array":
            return self.array_sql("VARCHAR")
        raise ValueError(f"dialect {self.name}: unknown cast target {spec!r}")


def _backtick(name: str) -> str:
    return f"`{name}`"


def _dquote(name: str) -> str:
    # Escape embedded double quotes (SQL standard doubling); without this an
    # identifier containing '"' emits broken SQL on postgres/snowflake.
    return f'"{name.replace(chr(34), chr(34) * 2)}"'


def _bare(name: str) -> str:
    return name


def _dec(p: int, s: int) -> str:
    return f"DECIMAL({p},{s})"


def _array(elem: str) -> str:
    return f"{elem}[]"


def _bq_dec(p: int, s: int) -> str:
    return f"NUMERIC({p},{s})"


def _bq_array(elem: str) -> str:
    return f"ARRAY<{elem}>"


def _sf_dec(p: int, s: int) -> str:
    return f"NUMBER({p},{s})"


def _sf_array(elem: str) -> str:
    return "ARRAY"


# Functions that every dialect expresses identically, spelled how each
# warehouse expects the CALL to look in SQL. This map is the last word on
# spelling: functions.py declares signatures, and a dialect overrides the
# default spelling here (e.g. BigQuery naming a function differently).
# Window functions spell identically everywhere (ROW_NUMBER, RANK, LAG, ...).
# split_part is special: BigQuery has no native SPLIT_PART, so it is
# emulated with SPLIT(...)[SAFE_OFFSET(...)] in sqlgen (which returns NULL
# for an out-of-range part instead of '').
_UNIVERSAL_FNS = {
    "count": "COUNT", "sum": "SUM", "avg": "AVG", "max": "MAX", "min": "MIN",
    "coalesce": "COALESCE", "upper": "UPPER", "lower": "LOWER", "concat": "CONCAT",
    "row_number": "ROW_NUMBER", "rank": "RANK", "dense_rank": "DENSE_RANK",
    "lag": "LAG", "lead": "LEAD", "first_value": "FIRST_VALUE",
    "last_value": "LAST_VALUE",
    "length": "LENGTH", "substring": "SUBSTRING", "trim": "TRIM",
    "replace": "REPLACE", "left": "LEFT", "right": "RIGHT",
    "regexp_replace": "REGEXP_REPLACE", "split_part": "SPLIT_PART",
    "startswith": "STARTS_WITH",
}

DUCKDB = Dialect(
    "duckdb", _bare,
    {"int64": "BIGINT", "float64": "DOUBLE", "string": "VARCHAR", "bool": "BOOLEAN",
     "date": "DATE", "timestamp": "TIMESTAMP", "uuid": "UUID", "json": "JSON"},
    _dec, "DECIMAL(38,2)", _array, supports_anti_semi=True,
    function_map=_UNIVERSAL_FNS,
)

BIGQUERY = Dialect(
    "bigquery", _backtick,
    {"int64": "INT64", "float64": "FLOAT64", "string": "STRING", "bool": "BOOL",
     "date": "DATE", "timestamp": "TIMESTAMP", "uuid": "STRING", "json": "JSON"},
    _bq_dec, "NUMERIC(38,2)", _bq_array, supports_anti_semi=False,
    # Standard GoogleSQL: no STARTS_WITH (pattern: prefix LIKE '...%'),
    # no SPLIT_PART (emulated with SPLIT in functions.emit_sql), no LPAD/RPAD.
    # Unavailable functions are mapped to a *_UNAVAILABLE spelling: codegen
    # never emits it silently — the call raises (fail-loud dialect gap).
    function_map={**_UNIVERSAL_FNS, "startswith": "LIKE_PREFIX",
                  "split_part": "SPLIT", "lpad": "LPAD_UNAVAILABLE",
                  "rpad": "RPAD_UNAVAILABLE"},
)

SNOWFLAKE = Dialect(
    "snowflake", _dquote,
    {"int64": "BIGINT", "float64": "DOUBLE", "string": "VARCHAR", "bool": "BOOLEAN",
     "date": "DATE", "timestamp": "TIMESTAMP_NTZ", "uuid": "VARCHAR", "json": "VARIANT"},
    _sf_dec, "NUMBER(38,2)", _sf_array, supports_anti_semi=False,
    # Snowflake names startswith STARTSWITH (no underscore).
    function_map={**_UNIVERSAL_FNS, "startswith": "STARTSWITH"},
)

def _pg_dec(p: int, s: int) -> str:
    return f"NUMERIC({p},{s})"


# Postgres: double-quoted identifiers, TEXT for strings, JSONB for json
# (the idiomatic postgres json column: indexable, deduplicated; plain JSON
# only matters when key order/duplicates must be preserved, which the typed
# DAG does not promise). No ANTI/SEMI JOIN keywords -> fail-loud like
# BigQuery/Snowflake; rewrite the model with NOT EXISTS.
POSTGRES = Dialect(
    "postgres", _dquote,
    {"int64": "BIGINT", "float64": "DOUBLE PRECISION", "string": "TEXT", "bool": "BOOLEAN",
     "date": "DATE", "timestamp": "TIMESTAMP", "uuid": "UUID", "json": "JSONB"},
    _pg_dec, "NUMERIC(38,2)", _array, supports_anti_semi=False,
    function_map=_UNIVERSAL_FNS,
)

_DIALECTS = {d.name: d for d in (DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE)}


def get_dialect(name: str) -> Dialect:
    try:
        return _DIALECTS[name]
    except KeyError:
        raise ValueError(f"unknown dialect {name!r} (have: {', '.join(_DIALECTS)})") from None


def physical_type(dialect: Dialect, t: StrataType) -> str:
    """Return the physical storage type for a StrataType in dialect.

    Raises ValueError if the type cannot be expressed in the dialect
    (e.g. an array of an unsupported element, or an unknown base type)."""
    if t.name == "unknown":
        raise ValueError(f"dialect {dialect.name}: cannot express unknown type")
    if t.name == "decimal":
        return dialect.decimal_sql(t.precision, t.scale)
    if t.name == "money":
        return dialect.money
    if t.name == "array":
        if t.elem is None:
            raise ValueError(f"dialect {dialect.name}: array without element type")
        return dialect.array_sql(physical_type(dialect, t.elem))
    sql = dialect.sql_type(t.name)
    if sql is None:
        raise ValueError(f"dialect {dialect.name}: no physical type for {t.name}")
    return sql
