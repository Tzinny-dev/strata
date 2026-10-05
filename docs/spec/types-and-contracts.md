# Strata — Type System, Contracts and Lineage Rules

Formal rules that the `typecheck` and `lineage` passes of the reference implementation implement.

---

## 1. Value types

```
T := Int64 | Float64 | Decimal(p,s,p>) | String | Bool | Date | Timestamp
  | Uuid | Json | Money(currency) | Array(T) | ColumnSet
```

- `Money(C)` is `Decimal(38,2)` refined by currency; arithmetic between two `Money(C)` must share `C`, result `Money(C)`. Mixed currency = compile error (echo of the dimensional-safety gap, §4.1 of research).
- Nullability is a *separate flag* on a column: `Col = (T, nullable: bool)`.
- `Unknown` is the initial type of unresolved columns; it only exists mid-inference and must resolve before codegen.

Subtyping (`<:`):
```
Int64 <: Float64 <: Decimal(38,?)  (via implicit but explicit-safe widening through cast() only)
Date <: Timestamp                  (only via cast)
Array(T) <: Array(U)  iff T = U     (invariant)
Money(C) <: Decimal(38,2) when C discarded explicitly (cast only)
```
The field of narrowing (e.g. `Float64` returned where `Decimal` was promised) is always a cast error at the boundary — **narrowing never happens implicitly**.

## 2. Operator typing rules

Let `⊕` range over `+ - * / %`, `≶` over `== != < <= > >=`.

```
lit(n:int)      : Int64, nonnull
lit(x:float)    : Float64, nonnull
lit(s:str)      : String, nonnull
lit(bool)       : Bool, nonnull
lit(none)       : Unknown, nullable

x ⊕ y  (numeric) : Float64                            if either is Float64|Decimal
                   Decimal(p,s)                       if both Decimal and ops are + - *
                   Int64                              otherwise (integer ops)
                   nullability:  nullable(y) iff nullable(x) or nullable(y)
x / y            : Float64 always (no integer division)

x + y  (String)  : String, nullable OR  (via '||' sugar)
concat(Array)    : T, nullable

coalesce(x,y)    : (type(x) ⊔ type(y)), nullable=false
                   (if any argument nonnull the result is judged nonnull; tightened by runtime pin)

upper/lower(x)   : String, nullability(x)
count(...)       : Int64, nonnull           -- never null, empty group ⇒ 0
sum(x)           : Int64|Float64|Decimal (same as x), nullable=true  -- empty group ⇒ NULL
avg(x)           : Float64, nullable=true
max/min(x)       : type(x), nullability(x)   -- of non-empty else null: marked nullable

x ≶ y            : Bool, nullable = nullable(x) or nullable(y)
not/neg          : preserve nullability of operand
case/if          : result type = least upper bound of branch types; nullable = OR of branches
```

`Unknown ⊕ T` etc. is an error surfaced as "unresolved column".

## 3. Domain/semantic annotations (contract layer)

A `contract` field is `Col = (T, {nullable, unique, primary, protected, enum:Set,String>, classification, partition_by, freshness})`.

Derived rules:
- `primary_key ⇒ nonnull, unique`.
- `protected` ⇒ removing/renaming/narrowing this column in an *upstream* change is a **breaking change** for every consumer referencing it (E-family error in the producer's PR).
- `enum {a,b,c}` ⇒ any expression feeding it must be proven to range over a subset (string equality against enum literals is tracked; otherwise runtime pin).
- `unique` ⇒ the model must contain the column in a `group`/`aggregate` producing one row per value; the planner adds a uniqueness assertion.
- `classification: str` ⇒ maps to a masking rule at the storage boundary; absence of `mask` when classification present is a lint warning (W004-style).

## 4. Column-level lineage

Lineage is a **property of the AST/semantic graph**, not a post-hoc parser.

Definitions:
- A lineage path is a tuple `(node_id, column, transform_kind)`, where `transform_kind ∈ {passthrough, derived, aggregated, renamed, grouped, filtered}`.
- Each output column of a `model` carries `Origin = List[Path]`.
- Rules:
  1. `select { x = col }` ⇒ `Origin(x) = Origin(col)` — identity, `passthrough`.
  2. `derive { y = f(cols...) }` ⇒ `Origin(y) = ⋃ Origin(c) for c ∈ cols`, `derived`.
  3. `aggregate { s = sum(x), g = group_key }` ⇒ `Origin(s) = [Origin(x)]` kind `aggregated`; `Origin(g) = [Origin(g)]` kind `grouped`.
  4. `join L on cond` ⇒ all columns from the left side keep their origin; right columns keep their origin but are marked `joined`; join predicates record lineage edges for both sides (used for join-cardinality checks).
  5. `filter cond` ⇒ does not change origins; contributes *dependency edges* (the model reads those columns).
  6. Pass-through of `SELECT *` is expanded against the upstream schema at compile time (never at runtime).

## 5. Contract conformance (three-phase enforcement)

Phase **A — authoring (compile-time/lint in the editor)**:
- Inferred output columns must be a superset of required contract fields (missing ⇒ `E010`).
- Type conformance: promised `T` must equal inferred `T`, or inferred `<:` promised, or there is an explicit `cast` (else `E011`).
- Nullability: promised `nonnull` requires inferred `nonnull` (else `E012`).
- `primary_key` present and non-implicit (`E013` if a protected column is missing).

Phase **B — planning (compile-time against catalog reality)**:
- Every `ref`/`source` resolves to a known schema *in the current catalog*; a column that moved (renamed/narrowed upstream since a different run) is an error `E030–E032`, generated in the *producer*'s plan via the cross-project contract.
- Blast-radius: reverse lineage edge set `Down(m,c)` = all models that `ref` a column whose origin includes `m.c`. `strata lineage-diff base..head` yields exactly `Down` named per column.

Phase **C — runtime (write pin before materialization)**:
- Re-check the actual physical schema (Arrow/CSV/untrusted input) against the contract: types, nullability, enum membership, nested-collection optionality, uniqueness assertion, freshness.
- Violation ⇒ abort the *apply*; last-known-good stays live (blue-green contract §6 of compiler design).

## 6. Determinism & fingerprinting

- `Fingerprint(model) = H(model_source) ⊕ ⋃_upstream Fingerprint(u) ⊕ H(schema-version)` (content-addressed, not time-addressed).
- A model is **stale** if `Fingerprint(now) ≠ Fingerprint(last_apply)`; `strata plan` recomputes only stale models (topological minimal set).
- Same-stale-set + same upstream layers ⇒ byte-identical artifact (given deterministic function set; non-deterministic functions such as `rand` are banned at the type level except behind explicit `seed`).

## 7. Grammar-context for LLM generation

For grammar-constrained decoding the valid-token set at each position is derived from a GBNF encoding of `spec/grammar.md`; the type checker then *rejects* (before codegen) programs whose output violates any contract, so an LLM cannot emit a syntactically valid program that silently breaks lineage/contracts (mirror of PyDough's "cannot express wrong join").