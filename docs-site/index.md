# Strata

**Declarative, versioned, immutable data transformations — compiles to SQL** (DuckDB/Postgres/BigQuery/Snowflake), with column-level lineage and 3-phase contract pins.

> Working title v0.1 — `strata-lang 0.1.1` on PyPI · `strata 34M` standalone · `ghcr.io/tzinny-dev/strata`

<div class="tip">

```bash
pip install strata-lang
strata --help
# or standalone
curl -fsSL https://raw.githubusercontent.com/Tzinny-dev/strata/main/install.sh | bash
strata build examples/daily_orders.strata
```

</div>

## Quick links

- **Getting Started** — `strata check/build/run` with real CLI output
- **Tutorial** — end-to-end verified walkthrough
- **Reference** — syntax, contracts, incremental, setops, JSON/arrays
- **Spec** — grammar + types (normative)
- **Changelog** — 0.1.0, 0.1.1, binary Fase 0-4

## Install

::: code-group

```bash [pip]
pip install strata-lang
strata --help
```

```bash [uv]
uv tool install strata-lang
strata --help
```

```bash [standalone]
curl -fsSL https://raw.githubusercontent.com/Tzinny-dev/strata/main/install.sh | bash
strata --help
```

```bash [docker]
docker pull ghcr.io/tzinny-dev/strata:0.1.1
docker run --rm ghcr.io/tzinny-dev/strata:0.1.1 --help
```

:::

## Status

[![CI](https://github.com/Tzinny-dev/strata/actions/workflows/ci.yml/badge.svg)](https://github.com/Tzinny-dev/strata/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/strata-lang)](https://pypi.org/project/strata-lang/) [![Coverage 81%](https://img.shields.io/badge/coverage-81%25-brightgreen)](/htmlcov/index.html)

Prototype: `537 tests, 81% coverage`, `strata.spec` onefile `34M`, `bench all green`.

## Next

See [Getting Started](/guide/getting-started) — every code block was run against the CLI before writing.
