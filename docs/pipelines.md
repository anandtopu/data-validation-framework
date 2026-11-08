# In-pipeline validation

`dvf.validate_dataframe` checks a DataFrame inside a data pipeline and splits its rows by the result. It works with pandas, Polars, Arrow, and Spark DataFrames:

```python
import dvf

valid, quarantine, result = dvf.validate_dataframe(
    orders,
    checks=[
        {"type": "not_null", "columns": ["order_id", "customer_id"]},
        {"type": "regex", "column": "country", "pattern": "^[A-Z]{2}$"},
        {"type": "range", "column": "amount", "min": 0, "action": "drop"},
        {"type": "unique", "columns": ["order_id"], "action": "warn"},
        {"type": "row_count_range", "min": 1},
    ],
    on_fail="quarantine",
    keys=["order_id"],
)
```

- `valid` holds the rows that passed (or failed only `warn` or `fail` checks), as the same DataFrame type you passed in. pandas indexes and dtypes are kept.
- `quarantine` holds the quarantined rows plus a `dvf_reasons` column that names the failed checks, for example `not_null.customer_id; regex.country`.
- `result` holds every check's result, with the same fields as in a `dvf run` results document: status, counts, masked samples, and the SQL that ran.

Checks are written exactly as in a configuration file's `checks:` list, as dictionaries or as the check models in `dvf.config.checks`. Plugin checks work too. Nothing is written to disk: no reports, history, or checkpoints.

## What happens to failing rows

Each check's `action` decides. Checks without an `action` use the call's `on_fail`, which defaults to `quarantine`.

| Action | Failing rows | Check result |
|---|---|---|
| `quarantine` | Moved to the quarantine frame | Reported by severity |
| `drop` | Removed from both frames | Reported by severity |
| `warn` | Kept in the valid frame | At most a warning |
| `fail` | Kept in the valid frame | Raises `DataValidationError` when the check fails |

A row that fails several checks is quarantined if any of them quarantines it. Otherwise it is dropped if any of them drops it.

Rows are marked by row-level checks (`not_null`, `not_blank`, `regex`, `allowed_values`, `range`, `length`, `expression`, and plugin `RowCheck` subclasses) and by `unique`, which marks every row of a duplicated key. Dataset-level checks (`row_count_range`, `null_ratio`, `freshness`, `custom_sql`, `referential_integrity`) only affect the result.

A row is marked even when its check passes because of `mostly`. The check passes, but the row is still routed by the check's action. Rows outside a check's `where` filter are never marked by that check.

`DataValidationError` is also raised when a check cannot be evaluated, for example because a column does not exist, so a broken check never lets rows through silently. The exception's `result` attribute holds every check's result.

## Options

| Option | Default | Meaning |
|---|---|---|
| `on_fail` | `quarantine` | Action for checks without their own `action` |
| `name` | `dataframe` | Dataset name in results and check IDs |
| `keys` | none | Columns shown first in samples |
| `severity` | `error` | Default severity of the checks |
| `sample_size` | `20` | Failing rows captured per check (masked) |
| `privacy` | standard | `defaults.privacy` settings, for example `{"mode": "strict"}` for no samples |
| `run_id` | generated | Run ID recorded in the result |

## Spark and Structured Streaming

A Spark DataFrame stays in Spark. The checks run as Spark SQL, and the valid and quarantine frames are lazy Spark DataFrames, not collected rows.

For Structured Streaming, `dvf.foreach_batch_validator` returns a function for `foreachBatch` that validates each micro-batch and passes the parts to your callbacks:

```python
def write_valid(df, batch_id):
    df.write.mode("append").saveAsTable("silver.orders")


def write_quarantine(df, batch_id):
    df.write.mode("append").saveAsTable("quarantine.orders")


query = (
    spark.readStream.table("bronze.orders")
    .writeStream.foreachBatch(
        dvf.foreach_batch_validator(checks, on_valid=write_valid, on_quarantine=write_quarantine)
    )
    .start()
)
```

`on_result` receives each batch's results, for example to log them or send metrics. The function works with any DataFrame type, so you can unit-test it with a small pandas frame.

## Limits

- pandas, Polars, and Arrow frames are queried in memory through DuckDB without copying. Collect a Polars `LazyFrame` first.
- Column names `dvf_row_id` and `dvf_reasons` are reserved.
- In `dvf run`, the `action` field is ignored and failures are reported by severity.
