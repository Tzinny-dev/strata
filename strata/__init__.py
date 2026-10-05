"""Strata -- declarative, versioned, immutable data transformations."""

__version__ = "0.1.5"

# Core parsing
from strata.parser import parse_strata, ParseError
from strata.lexer import LexError

# AST nodes
from strata.ast import (
    Module,
    SourceDecl,
    ModelDecl,
    ContractDecl,
    TestDecl,
    SelectStmt,
    FilterStmt,
    LetStmt,
    JoinStmt,
    GroupStmt,
    AggregateStmt,
    SortStmt,
    BinOp,
    Call,
    ColumnRef,
    Literal,
    WindowSpec,
    WindowCall,
)

# Types
from strata.types import (
    StrataType,
    INT64,
    FLOAT64,
    STRING,
    BOOL,
    DATE,
    TIMESTAMP,
    UUID,
    JSON,
    UNKNOWN,
    decimal,
    money,
    array,
    map_type,
    struct_type,
    unify,
    Col,
    Schema,
)

# Analysis
from strata.analysis import (
    StrataError,
    Project,
    Checker,
    TypedModel,
    InputSpec,
    JoinSpec,
    Plan,
    PlanOut,
    build_down_edges,
    blast_radius,
    type_from_spec,
)

# Dialects
from strata.dialects import (
    Dialect,
    get_dialect,
    physical_type,
    DUCKDB,
    POSTGRES,
    BIGQUERY,
    SNOWFLAKE,
)

# SQL Generation
from strata.sqlgen import gen_outer, model_sql, full_sql

# Execution
from strata.exec import (
    PinError,
    with_lock,
    record_run,
    load_history,
    find_run,
    stale_models,
    load_manifest,
    save_manifest,
    runtime_pins,
    check_physical_schema,
    staged_name,
    promoted_name,
    physical_schema,
    RunResult,
    run,
    execute_run,
)

# Formatter
from strata.fmt import format_module

# Seed
from strata.seed import seed_sql

# Adapters
from strata.adapters import get_adapter, Warehouse

# Import DBT
from strata.importdbt import import_dbt_schema

# Observability
from strata.exec import get_metrics
from strata.observability import MetricsCollector

__all__ = [
    # Version
    "__version__",
    # Parsing
    "parse_strata",
    "ParseError",
    "LexError",
    # AST
    "Module",
    "SourceDecl",
    "ModelDecl",
    "ContractDecl",
    "TestDecl",
    "SelectStmt",
    "FilterStmt",
    "LetStmt",
    "JoinStmt",
    "GroupStmt",
    "AggregateStmt",
    "SortStmt",
    "BinOp",
    "Call",
    "ColumnRef",
    "Literal",
    "WindowSpec",
    "WindowCall",
    # Types
    "StrataType",
    "INT64",
    "FLOAT64",
    "STRING",
    "BOOL",
    "DATE",
    "TIMESTAMP",
    "UUID",
    "JSON",
    "UNKNOWN",
    "decimal",
    "money",
    "array",
    "map_type",
    "struct_type",
    "unify",
    "Col",
    "Schema",
    # Analysis
    "StrataError",
    "Project",
    "Checker",
    "TypedModel",
    "InputSpec",
    "JoinSpec",
    "Plan",
    "PlanOut",
    "build_down_edges",
    "blast_radius",
    "type_from_spec",
    # Dialects
    "Dialect",
    "get_dialect",
    "physical_type",
    "DUCKDB",
    "POSTGRES",
    "BIGQUERY",
    "SNOWFLAKE",
    # SQL Generation
    "gen_outer",
    "model_sql",
    "full_sql",
    # Execution
    "PinError",
    "with_lock",
    "record_run",
    "load_history",
    "find_run",
    "stale_models",
    "load_manifest",
    "save_manifest",
    "runtime_pins",
    "check_physical_schema",
    "staged_name",
    "promoted_name",
    "physical_schema",
    "RunResult",
    "run",
    "execute_run",
    # Formatter
    "format_module",
    # Seed
    "seed_sql",
    # Adapters
    "get_adapter",
    "Warehouse",
    # Import DBT
    "import_dbt_schema",
    # Observability
    "get_metrics",
    "MetricsCollector",
]