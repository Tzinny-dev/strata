"""Dialect adapters: identifier quoting + type maps per target warehouse.

The main job of the adapter is not "prettier SQL" — it is the Strata guarantee
that "the same typed DAG compiles and runs on any warehouse you pin to."
Where a dialect cannot express something the plan declares (e.g. Snowflake
and BigQuery have no ANTI/SEMI JOIN keyword), the translator must fail loudly
instead of emitting a silently-wrong query.
"""
from __future__ import annotations

from typing import Dict, Optional


class Dialect:
    def __init__(self, name: str, quote_ident, type_map: Dict[str, str],
                 decimal, money: str, array, supports_anti_semi: bool):
        self.name = name
        self._quote = quote_ident
        self.type_map = type_map
        self._decimal = decimal
        self._array = array
        self.money = money
        self.supports_anti_semi = supports_anti_semi

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
    return f'"{name}"'


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
    return f"ARRAY"


DUCKDB = Dialect(
    "duckdb", _bare,
    {"int64": "BIGINT", "float64": "DOUBLE", "string": "VARCHAR", "bool": "BOOLEAN",
     "date": "DATE", "timestamp": "TIMESTAMP", "uuid": "UUID", "json": "JSON"},
    _dec, "DECIMAL(38,2)", _array, supports_anti_semi=True,
)

BIGQUERY = Dialect(
    "bigquery", _backtick,
    {"int64": "INT64", "float64": "FLOAT64", "string": "STRING", "bool": "BOOL",
     "date": "DATE", "timestamp": "TIMESTAMP", "uuid": "STRING", "json": "JSON"},
    _bq_dec, "NUMERIC(38,2)", _bq_array, supports_anti_semi=False,
)

SNOWFLAKE = Dialect(
    "snowflake", _dquote,
    {"int64": "BIGINT", "float64": "DOUBLE", "string": "VARCHAR", "bool": "BOOLEAN",
     "date": "DATE", "timestamp": "TIMESTAMP_NTZ", "uuid": "VARCHAR", "json": "VARIANT"},
    _sf_dec, "NUMBER(38,2)", _sf_array, supports_anti_semi=False,
)

DIALECTS = {d.name: d for d in (DUCKDB, BIGQUERY, SNOWFLAKE)}


def get_dialect(name: str) -> Dialect:
    try:
        return DIALECTS[name]
    except KeyError:
        raise ValueError(f"unknown dialect {name!r} (have: {', '.join(DIALECTS)})")
