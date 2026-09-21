"""Dialect adapters: identifier quoting + type maps per target warehouse.

The main job of the adapter is not "prettier SQL" — it is the Strata guarantee
that "the same typed DAG compiles and runs on any warehouse you pin to."
Where a dialect cannot express something the plan declares (e.g. Snowflake
and BigQuery have no ANTI/SEMI JOIN keyword), the translator must fail loudly
instead of emitting a silently-wrong query.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional


from .types import StrataType


class Dialect:
    """Render rules for one warehouse dialect (identifier quoting, physical
    types, function overrides, optional partitioning support)."""

    def __init__(self, name: str, quote_ident: Callable[[str], str],
                 type_map: Dict[str, str],
                 decimal: Callable[[int, int], str], money: str,
                 array: Callable[[str], str], supports_anti_semi: bool,
                 function_map: Optional[Dict[str, str]] = None,
                 supports_partitioning: bool = False,
                 partition_clause: Optional[Callable[[List[str]], str]] = None) -> None:
        self.name = name
        self._quote = quote_ident
        self.type_map = type_map
        self._decimal = decimal
        self._array = array
        self.money = money
        self.supports_anti_semi = supports_anti_semi
        self.function_map = function_map or {}
        self.supports_partitioning = supports_partitioning
        self._partition_clause = partition_clause

    # -- identifiers --------------------------------------------------
    def ident(self, name: str) -> str:
        """Quote a bare identifier for this dialect (e.g. ``"name"`` / `` `name` ``)."""
        return self._quote(name)

    def qualified(self, alias: str, name: str) -> str:
        """Emit ``alias.<quoted identifier>``."""
        return f"{alias}.{self.ident(name)}"

    # -- types ----------------------------------------------------------
    def sql_type(self, name: str) -> Optional[str]:
        """Physical SQL type for a logical type name, or None if unknown."""
        return self.type_map.get(name)

    def decimal_sql(self, p: int, s: int) -> str:
        """Physical SQL for a DECIMAL(p, s)."""
        return self._decimal(p, s)

    def array_sql(self, elem_sql: str) -> str:
        """Physical SQL for an array over the given element SQL."""
        return self._array(elem_sql)

    def cast_target(self, spec: str) -> str:
        """Physical SQL type for a cast() target spec string (logical name, money, decimal(p,s), array)."""
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

    # -- partitioning --------------------------------------------------
    def partition_clause(self, columns: List[str]) -> str:
        """Generate partitioning clause for CTAS if supported.

        Returns empty string if dialect doesn't support partitioning.
        """
        if not self.supports_partitioning or not columns:
            return ""
        if self._partition_clause:
            return self._partition_clause(columns)
        return ""


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
    """Look up a Dialect by name, raising ValueError with the known set if absent."""
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
    if t.name == "map":
        if t.key is None or t.value is None:
            raise ValueError(f"dialect {dialect.name}: map without key/value types")
        if dialect.name == "duckdb":
            # A typed map<string, V>: native DuckDB MAP with VARCHAR keys. The
            # value type is already a JSON-representable scalar (analysis), so
            # it always has a physical spelling.
            return f"MAP(VARCHAR, {physical_type(dialect, t.value)})"
        # The other warehouses have no MAP type: the map is backed by their
        # JSON type (JSONB / JSON / VARIANT), which is exactly the shape both
        # the constructor and map_get emit for it.
        json_sql = dialect.sql_type("json")
        if json_sql is None:
            raise ValueError(f"dialect {dialect.name}: no physical type for map<{t.key},{t.value}>")
        return json_sql
    sql = dialect.sql_type(t.name)
    if sql is None:
        raise ValueError(f"dialect {dialect.name}: no physical type for {t.name}")
    return sql