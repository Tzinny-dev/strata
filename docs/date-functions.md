# Calendar functions

```strata
date_add(d, years: 1)
date_sub(d, days: n + 1)
date_trunc(d, month)
date_diff(start_date, end_date, day)
```

- Shifts require exactly one named unit: `years`, `quarters`, `months`,
  `weeks`, or `days`. Amounts are integer expressions, including negative
  and nullable values. Multiple units require nested calls; ordering matters.
- Truncation and differences accept `year`, `quarter`, `month`, `week`, `day`
  as a bare symbol or a quoted string. Differences also accept `weeks`.
  Unit symbols are contextual; ordinary columns named `day` or `month`
  are not reserved.
- Shifts and truncation preserve the first argument's `date` or `timestamp`
  type. Null values propagate, including a null shift amount.
- Month/quarter/year shifts clamp to the last valid day of the destination
  month: `2024-02-29 + 1 year = 2025-02-28`. They do not preserve an
  end-of-month flag: `2024-02-29 + 1 month = 2024-03-29`.
- `date_diff(start, end, unit)` returns an `int64` count of calendar boundaries
  crossed, not elapsed complete durations. Both temporal arguments must have
  the same type. Reversing them reverses the sign. Weeks start Monday.
- Timestamp calculations use UTC civil time. BigQuery conversions explicitly
  use UTC; other targets use Strata's timezone-free timestamp SQL types.
  Sub-day units and timezone-aware arithmetic are outside this surface.

The typed plan retains each date call's base type for SQL generation.
DuckDB/PostgreSQL interval results are cast back to DATE when needed.
BigQuery uses DATE/DATETIME operations; Snowflake uses DATEADD/DATEDIFF.
Snowflake week operations avoid dependence on the session's WEEK_START.

Diagnostics: E062 arity, E063 argument kind, E071 unsupported unit,
E072 invalid keyword/unit shape, E073 mismatched date_diff temporal types.
Untyped date calls cannot use the generic SQL function emitter.

Validation: `tests/test_date_functions.py` executes calendar and result-type
checks on DuckDB and checks SQL emission for all four dialects. It does not
execute against live BigQuery, Snowflake, or PostgreSQL services.
