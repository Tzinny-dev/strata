"""Strata value types and column/contract descriptors."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional, Set, List


@dataclass(frozen=True)
class StrataType:
    name: str = "unknown"
    currency: Optional[str] = None
    elem: Optional["StrataType"] = None
    precision: int = 38
    scale: int = 2

    def __str__(self) -> str:
        if self.name == "money":
            return f"money({self.currency})"
        if self.name == "decimal":
            return f"decimal({self.precision},{self.scale})"
        if self.name == "array":
            return f"array<{self.elem}>"
        return self.name

    def is_numeric(self) -> bool:
        return self.name in ("int64", "float64", "decimal")

    def is_money(self) -> bool:
        return self.name == "money"


INT64 = StrataType("int64", scale=0)
FLOAT64 = StrataType("float64")
STRING = StrataType("string")
BOOL = StrataType("bool")
DATE = StrataType("date")
TIMESTAMP = StrataType("timestamp")
UUID = StrataType("uuid")
JSON = StrataType("json")
UNKNOWN = StrataType("unknown")


def decimal(p: int = 38, s: int = 2) -> StrataType:
    return StrataType("decimal", precision=p, scale=s)


def money(cur: str = "USD") -> StrataType:
    return StrataType("money", currency=cur)


def array(elem: StrataType) -> StrataType:
    return StrataType("array", elem=elem)


# ---------------------------------------------------------------- types ops

def binary_type(op: str, lt: StrataType, rt: StrataType) -> StrataType:
    if op == "||":
        return STRING if (lt.name == rt.name == "string") else UNKNOWN

    if lt.is_money() or rt.is_money():
        if lt.is_money() and rt.is_money():
            if lt.currency != rt.currency:
                raise TypeError(
                    f"currency mismatch: {lt} vs {rt} -- mixed currencies are a compile error"
                )
            if op in ("+", "-"):
                return money(lt.currency)
            if op == "*":  # uncommon but allow with same currency
                return money(lt.currency)
            if op == "/":
                return FLOAT64
            return lt  # %
        # money op non-money
        if op == "*":
            if rt.is_numeric() or lt.is_numeric():
                c = lt if lt.is_money() else rt
                return money(c.currency)
            return UNKNOWN
        if op in ("+", "-"):
            return UNKNOWN  # e.g. money + int is nonsense unless scaling
        return UNKNOWN

    if op in ("+", "-", "*", "%"):
        if lt.name == rt.name == "int64":
            return INT64
        if lt.name in ("int64", "float64") and rt.name in ("int64", "float64"):
            return FLOAT64
        if lt.name == "decimal" and rt.name == "decimal":
            return decimal(min(lt.precision, rt.precision), min(lt.scale, rt.scale))
        if lt.name == "decimal" or rt.name == "decimal":
            return decimal()
        return UNKNOWN
    if op == "/":
        if lt.is_numeric() and rt.is_numeric():
            return FLOAT64
        if lt.is_money() and rt.is_money():
            return FLOAT64
        return UNKNOWN
    return UNKNOWN


# ------------------------------------------------------------ columns

@dataclass
class Col:
    name: str
    t: StrataType = UNKNOWN
    nullable: bool = True
    unique: bool = False
    primary: bool = False
    protected: bool = False
    enum: frozenset = frozenset()
    classification: Optional[str] = None

    def clone(self, **kw) -> "Col":
        base = {
            "name": self.name,
            "t": self.t,
            "nullable": self.nullable,
            "unique": self.unique,
            "primary": self.primary,
            "protected": self.protected,
            "enum": self.enum,
            "classification": self.classification,
        }
        base.update(kw)
        return Col(**base)

    def describe(self) -> str:
        bits = [str(self.t)]
        if not self.nullable:
            bits.append("nonnull")
        if self.primary:
            bits.append("PK")
        elif self.unique:
            bits.append("unique")
        if self.enum:
            bits.append("enum{" + ",".join(sorted(self.enum)) + "}")
        if self.protected:
            bits.append("protected")
        if self.classification:
            bits.append(f"class={self.classification}")
        return f"{self.name}: {' '.join(bits)}"


@dataclass
class Schema:
    node: str
    cols: "OrderedDict[str, Col]" = field(default_factory=lambda: OrderedDict())

    def get(self, name: str) -> Optional[Col]:
        return self.cols.get(name)


from collections import OrderedDict  # noqa: E402  (placed after dataclass for clarity)