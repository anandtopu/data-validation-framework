# Debugging and operations

Every failed check carries its evidence: masked samples, per-column mismatch counts with a classification, the exact SQL that ran on each side, the normalization pipeline, and timings. The tools on this page help you go further without re-running the whole suite.

## See the plan before running

`dvf explain` prints, for each table:

- the datasets, keys, and paired columns, with each column's normalization pipeline;
- the comparison strategies and the engine;
- the partitions;
- the SQL each step would run: row counts, the row-level projection, the first checksum level, and check predicates.

It only looks up catalogs; no validation query runs.

```bash
dvf explain dvf.yaml --table customers
dvf explain dvf.yaml --format json > plan.json
```

## Trace one record

`dvf trace` fetches one record by key from both sides. For each column it shows:

- the raw value;
- the value after each normalizer;
- the canonical value, and whether the two sides match;

followed by the row hash on each side.

```bash
dvf trace dvf.yaml --table customers --key 1011
dvf trace dvf.yaml --table order_lines --key 4711 --key 3   # composite key, in key order
```

Keys are given as they appear in the source; dvf applies the key's normalizers before looking the record up.

## Drill into a finished run

Runs are saved under `.dvf/runs/<run_id>/` next to the configuration.

```bash
dvf history dvf.yaml                       # recent runs
dvf debug RUN_ID --config dvf.yaml         # failed and errored checks
dvf debug RUN_ID compare.column_mismatch --config dvf.yaml   # one check, in full
dvf debug reports/2026-11-07/RUN_ID/results.json row_hash    # or from a results file
```

A check ID may be shortened to its end when that is unambiguous.

## Rerun only what failed

```bash
dvf rerun RUN_ID                           # uses the configuration the run used
dvf run dvf.yaml --failed-from RUN_ID      # the same, with any other run options
```

Only the tables with failures run, and only their failed and errored checks are recorded.

## Extracts and repro files

For each failed check, dvf writes next to the reports:

| File | Contents |
|---|---|
| `extracts/<check>.parquet` (or `.csv`) | All fetched mismatched values (up to `comparison.max_mismatch_rows`) or failing rows (up to `reports.extracts.max_rows_per_check`), masked like samples |
| `repro/<check>/source.sql`, `target.sql` | The exact queries, in order, with their timings and row counts |
| `repro/<check>/check.yaml` | A configuration that extends yours and runs only this check: `dvf run repro/<check>/check.yaml` |

Strict privacy mode (`defaults.privacy.mode: strict`) writes no extracts. Turn extracts off with `reports.extracts.enabled: false`.

## Interrupted and killed runs

Finished tables and partitions are checkpointed as they complete.

- **Ctrl+C:** dvf finishes the tables already running, writes partial results (status `interrupted`), and exits with code 2.
- **A killed process:** the checkpoints remain.

Either way, continue the run under the same run ID:

```bash
dvf run dvf.yaml --resume RUN_ID
```

dvf refuses to resume when the configuration changed since the run started; pass `--force-resume` to continue anyway. Turn checkpoints off with `defaults.execution.checkpoints: false`.

## Run-to-run comparison and trends

With `history.enabled` (the default), each run is recorded in `.dvf/history.duckdb`. Reports then show:

- new, resolved, and persisting failures against the baseline (`history.baseline`: `last_passed` by default, or `last_run`, `none`, or a run ID; override with `--baseline`);
- the quality score of earlier runs.

`history.retention_runs` keeps only the newest runs (history rows and checkpoint folders). Disable history for one run with `--no-history`.

## In a notebook

```python
import dvf.debug

run = dvf.debug.load("20261107T101500Z-3f9a", config="dvf.yaml")
run.failed()  # failed and errored checks
check = run.check("customers.compare.column_mismatch")
check.mismatches(format="pandas")  # the extract, or the samples
check.queries()  # the SQL that ran
```

`mismatches()` returns an Arrow table by default, or a pandas or Polars frame when those libraries are installed.

## Logs

`--log-level debug` adds detail. `--log-format json` writes one JSON object per line; each record includes `run_id`, `suite`, and `table` when it comes from a table's work, so logs from parallel tables can be told apart. `--keep-temp` keeps the local engine's spill folder for inspection.
