# Strata — Formal Grammar (EBNF)

Working title version 0.1. This grammar is the normative surface syntax. The reference implementation's parser (`prototype/`) follows it.

Conventions:
- `"..."` literal terminal
- `{ X }` zero or more, `[ X ]` optional, `( A | B )` choice
- `IDENT` = `[a-zA-Z_][a-zA-Z0-9_]*`
- `STR` = double-quoted string with `\"` and `\\` escapes
- `INT`, `FLOAT`, comments `//` and `/* */`, `#` line comment
- Keywords are lowercase; keywords are reserved (cannot be identifiers).

## 1. Program structure

```
strata_file     := { top_decl }
top_decl        := source_decl
                 | contract_decl
                 | model_decl
                 | pipeline_decl
                 | fn_decl
                 | import_decl

import_decl     := "import" path_ref                       -- imports another .strata module
path_ref        := IDENT { "." IDENT }                     -- e.g. finanzas.ingresos
```

## 2. Sources

A `source` names an external table. Its `resource` clause is backend-specific data (a catalog/dataset pair, a DuckDB table, a Parquet path).

```
source_decl     := "source" IDENT [resource_clause] "{" source_props "}"
resource_clause := "(" named_arg { "," named_arg } ")"
named_arg       := IDENT ":" ( STR | IDENT | INT )
source_props    := { source_prop }
source_prop     := ( "freshness" ":" interval_expr
                   | "filter" ":" expr
                   | "schema" ":" STR                    -- optional declared schema file
                   )
interval_expr   := ">=" INT time_unit | "<=" INT time_unit
time_unit       := "d" | "h" | "m" | "s"                 -- day/hour/min/sec
```

Examples:
```
source orders(ns: "crm", dataset: "prod_orders") {
  freshness: >= 1d
}
```

## 3. Contracts

A `contract` declares the typed, guaranteed shape of a table output.

```
contract_decl  := "contract" IDENT "{" contract_field { "," contract_field } "}"
contract_field := IDENT ":" type_ref { type_annot }   [ "default" ":" literal ]
type_ref       := prim_type
                | domain_type
prim_type      := "int64" | "float64" | "decimal" "(" INT "," INT ")"
                | "string" | "bool" | "date" | "timestamp"
                | "uuid" | "json" | "money" | "array" "<" type_ref ">"
domain_type    := IDENT                                -- user domain alias (e.g. money)
type_annot     := "nonnull"
                | "unique"
                | "primary_key"
                | "protected"
                | "enum" "{" STR { "," STR } "}"
                | "classification" ":" STR
                | "partition_by"
                | "freshness" ":" interval_expr
```

Constraints:
- `primary_key` implies `nonnull` and `unique`.
- `enum {..}` and `classification` are metadata attached to the type.

## 4. Models

A `model` is a pure function `Table → Table`. Optional output `contract` is mandatory in `strict` mode (default for projects).

```
model_decl     := "model" IDENT ["->" "contract" IDENT] "{" { model_attr } ( model_stmt | fn_call_expr ) { model_stmt } "}"
model_attr     := ( "owner" ":" STR
                  | "reason" ":" STR
                  | "description" ":" STR )
model_stmt     := from_stmt | join_stmt | filter_stmt | let_stmt
                | select_stmt | derive_stmt | aggregate_stmt
                | group_stmt | sort_stmt | take_stmt

from_stmt      := "from" [IDENT "."] IDENT                 -- schema-ref'd table/model
join_stmt      := ( "join_left" | "join_inner" ) [IDENT "."] IDENT "on" expr
                | "join_anti" | "join_semi" [IDENT "."] IDENT "on" expr
filter_stmt    := "filter" expr
let_stmt       := "let" IDENT "=" expr                    -- derives hidden column (visible at group/aggregate only)
select_stmt    := "select" "{" out_assign { "," out_assign } "}"
derive_stmt    := "derive" "{" out_assign { "," out_assign } "}"     -- same as let but persisted in output
aggregate_stmt := "aggregate" "{" out_assign { "," out_assign } "}"
group_stmt     := "group" "{" expr { "," expr } "}" "(" model_block ")"
sort_stmt      := "sort" "{" sort_key { "," sort_key } "}"
take_stmt      := "take" [INT ".."] [INT] | "take" INT

out_assign     := IDENT "=" expr
sort_key       := expr | "-" expr
model_block    := { model_stmt }
```

A model may also be produced by a compile-time `fn` and spliced into a pipeline (see §6).

## 5. Pipelines and environments

```
pipeline_decl  := "pipeline" IDENT "{" { pipeline_attr } { pipeline_entry } "}"
pipeline_attr  := ( "env" ":" IDENT
                  | "description" ":" STR )
pipeline_entry := ( "models" ":" list_of_refs
                  | "sources" ":" "{" source_override { "," source_override } "}" )
list_of_refs   := "[" path_ref { "," path_ref } "]"
source_override:= IDENT ":" "from" "(" named_arg { "," named_arg } ")"
```

## 6. Compile-time functions

`fn` bodies are pure compile-time computations over the *definition domain* (they produce `Model`/`Contract`/`Expr` objects, never run I/O at runtime).

```
fn_decl        := "fn" IDENT "(" params ")" "->" fn_ret_type "{" fn_body "}"
params         := [ IDENT ":" fn_ret_type { "," IDENT ":" fn_ret_type } ]
fn_ret_type    := "Model" | "List" "<" fn_ret_type ">" | "Str" | "Int" | "Bool"
                | "Contract" | "Expr" | "Table"
fn_body        := expr                          -- expression language (pure)
fn_call_expr   := IDENT "(" fn_args ")"         -- may appear where a model_stmt list is expected
fn_args        := [ expr { "," expr } ]
```

Expression language (used by both model expressions and fn bodies):

```
expr           := literal | column_ref | call_expr | unary | binary | "(" expr ")"
literal        := INT | FLOAT | STR | "true" | "false" | "none"
column_ref     := IDENT ["." IDENT]            -- bare name or schema-qualified
call_expr      := call_name "(" [expr { "," expr }] ")" [ "over" "(" window_spec { "," window_spec } ")" ]
call_name      := "count" | "sum" | "avg" | "max" | "min"
                | "coalesce" | "case" | "upper" | "lower" | "concat"
                | "cast" | "list" | "dict" | "map" | "if"
                | "length" | "substring" | "trim" | "ltrim" | "rtrim"
                | "replace" | "lpad" | "rpad" | "startswith" | "split_part"
                | "regexp_replace" | "left" | "right"
                | "like" | "rlike"
                | "date_add" | "date_sub" | "date_trunc" | "date_diff"
                | "json_get" | "json_value" | "array_length" | "array_get"
                -- prototype catalog (functions.py) implements strings, dates
                -- and basic collection access. JSON keys are simple literal
                -- object keys, not JSONPath; array_get uses zero-based indices.
                -- Date shifts accept unit kwargs (e.g. days: 1).
                -- if(cond, then, else) and case(cond, val, ..., [else]) are
                -- implemented (see prototype/docs -- conditional
                -- expressions), emitted as CASE WHEN...END. like/rlike are
                -- bool functions (LIKE and per-engine regexp), and `list` is
                -- an alias of array_construct: `list(1, 2)` ==
                -- `array_construct(1, 2)`. `dict`/`map` constructors and the
                -- `struct` type remain outside this supported slice; see
                -- prototype/docs/json-arrays.md.
window_spec    := "partition_by" ":" "[" [expr { "," expr}] "]"
                | "sort" ":" "[" [expr ["desc" | "asc"] { "," expr ["desc" | "asc"]}] "]"
unary          := ( "-" | "not" ) expr
binary         := expr binop expr
binop          := "==" | "!=" | "<" | "<=" | ">" | ">="
                | "+" | "-" | "*" | "/" | "%"
                | "and" | "or" | "||"          -- || : string concat
```

Precedence (low→high): `or`, `and`, comparison, `+ - ||`, `* / %`, unary, call/postfix.

## 7. Lexical grammar

```
IDENT      := [a-zA-Z_][a-zA-Z0-9_]*
INT        := [0-9]([0-9_]* [0-9])?             -- underscores allowed (1_000)
FLOAT      := [0-9]+ "." [0-9]+ | INT [eE] [+-]? [0-9]+
STR        := '"' ( char - ('"' | '\\') | escape )* '"'
escape     := '\\' ("n" | "t" | '"' | '\\')
comment    := "//" [^newline]* | "/*" .* "*/" | "#" [^newline]*
WS         := [ \t\r\n]+                        -- skipped; newlines significant for stmt boundaries
```

Keywords (reserved): `source contract model pipeline fn import from join_left join_inner join_anti join_semi on filter where let derive select aggregate group sort take env models sources owner reason description nonnull unique primary_key protected enum classification partition_by freshness namespace dataset schema and or not true false null in over`. `if`/`else`/`case` are ordinary identifiers (see `strata/lexer.py` `KEYWORDS`), not reserved: `if(...)`/`case(...)` parse as regular calls, the same as `coalesce`/`cast`.

## 8. Open questions (deferred)

- Streaming sources (`stream` keyword) — post-v1.
- Window functions (`over`): implemented in the prototype (parser + E065
  placement + `FN(...) OVER (...)` emission) despite this deferral; the spec
  text above now includes `window_spec`. Remaining to decide: frame clauses
  (`rows between ...`) and `lead`/`lag` offsets in the spec itself.
- Explicit casts between every narrowing (currently allowed via `cast` only).
- Annotation ordering is currently free; may be canonicalized by the formatter.