"""The Polars engine: raw values are fetched as Arrow, then normalized, canonicalized, and diffed
with Polars expressions (``pip install 'dvf-framework[polars]'``)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Literal

import pyarrow as pa

from dvf.compare.plan import PlannedColumn, SideExpr
from dvf.core.enums import Side
from dvf.core.errors import DependencyError
from dvf.engines.base import (
    DiffJob,
    DiffResult,
    DiffSide,
    Engine,
    MismatchRow,
)
from dvf.models.results import ReconciliationSummary
from dvf.normalize.canonical import canonical_polars, numeric_polars, polars_row_hash
from dvf.plugins.base import INPUT_COLUMN_PREFIX

if TYPE_CHECKING:
    import polars as pl


def _import_polars() -> Any:
    try:
        import polars as pl
    except ImportError as exc:
        raise DependencyError("polars", "polars", "The Polars engine") from exc
    return pl


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


class PolarsEngine(Engine):
    """Compares rows in Polars. Suits DataFrame-centric pipelines; DuckDB scales further."""

    name = "polars"

    def _raw_sql(
        self,
        columns: list[PlannedColumn],
        side: Literal["source", "target"],
        rel: str,
        inputs: tuple[str, ...] = (),
        quote: Any = None,
    ) -> str:
        items = []
        for i, col in enumerate(columns):
            expr = col.source if side == "source" else col.target
            items.append(f"{expr.raw} AS v{i}")
        for j, name in enumerate(inputs):
            items.append(f"{quote(name)} AS w{j}")
        return "SELECT " + ", ".join(items) + f" FROM {rel}"

    @staticmethod
    def _inputs(columns: list[PlannedColumn], side: Literal["source", "target"]) -> tuple[str, ...]:
        """Other columns the side's normalizers read (``Normalizer.input_columns``), in order."""
        names: dict[str, None] = {}
        for col in columns:
            expr = col.source if side == "source" else col.target
            for normalizer in expr.normalizers:
                names.update(dict.fromkeys(normalizer.input_columns()))
        return tuple(names)

    def _fetch(
        self, job: DiffJob, side: DiffSide, label: Side, columns: list[PlannedColumn]
    ) -> tuple[pl.DataFrame, float]:
        pl = _import_polars()
        which: Literal["source", "target"] = "source" if label is Side.SOURCE else "target"
        inputs = self._inputs(columns, which)
        sql = self._raw_sql(
            columns, which, side.relation.sql, inputs, side.connector.sql_dialect.quote
        )
        started = time.perf_counter()
        batches = list(side.connector.iter_batches(sql))
        table = (
            pa.Table.from_batches(batches)
            if batches
            else side.connector.query(side.connector.sql_dialect.limit(sql, 0))
        )
        table = table.rename_columns(
            [f"v{i}" for i in range(len(columns))] + [INPUT_COLUMN_PREFIX + n for n in inputs]
        )
        elapsed = _ms(started)
        job.recorder.add(side.connector, sql, label, elapsed, table.num_rows, engine=self.name)
        frame = pl.from_arrow(table)
        assert isinstance(frame, pl.DataFrame)
        return frame, elapsed

    def _project(
        self, frame: pl.DataFrame, job: DiffJob, which: Literal["source", "target"]
    ) -> pl.DataFrame:
        pl = _import_polars()
        plan = job.plan
        columns = plan.keys + plan.columns
        exprs: list[Any] = []
        canon_by_column: dict[int, Any] = {}

        def side(col: PlannedColumn) -> SideExpr:
            return col.source if which == "source" else col.target

        normalized: list[Any] = []
        for i, col in enumerate(columns):
            expr = pl.col(f"v{i}")
            ctype = side(col).raw_type
            for normalizer in side(col).normalizers:
                expr = normalizer.to_polars(expr, ctype)
                ctype = normalizer.output_type(ctype)
            normalized.append(expr.alias(f"z{i}"))
        staged = frame.with_columns(normalized)
        schema = staged.schema
        for i, col in enumerate(columns):
            spec = side(col).spec
            dtype = schema[f"z{i}"]
            raw_text = pl.col(f"v{i}").cast(pl.String, strict=False)
            if col.is_key:
                exprs.append(canonical_polars(pl.col(f"z{i}"), spec, dtype).alias(f"k{i}"))
                exprs.append(raw_text.alias(f"rk{i}"))
                continue
            j = i - len(plan.keys)
            if col.pair_spec.numeric is not None:
                exprs.append(
                    numeric_polars(pl.col(f"z{i}"), spec, col.pair_spec.numeric, dtype).alias(
                        f"n{j}"
                    )
                )
            else:
                canon = canonical_polars(pl.col(f"z{i}"), spec, dtype)
                canon_by_column[j] = canon
                exprs.append(canon.alias(f"c{j}"))
            exprs.append(raw_text.alias(f"r{j}"))
        projected = staged.select(exprs)
        hashed = [pl.col(f"c{j}") for j in sorted(canon_by_column)]
        return projected.with_columns(polars_row_hash(hashed).alias("h"))

    def diff(self, job: DiffJob) -> DiffResult:
        pl = _import_polars()
        plan = job.plan
        columns = plan.keys + plan.columns
        source_frame, source_ms = self._fetch(job, job.source, Side.SOURCE, columns)
        target_frame, target_ms = self._fetch(job, job.target, Side.TARGET, columns)
        started = time.perf_counter()
        s = self._project(source_frame, job, "source")
        t = self._project(target_frame, job, "target")
        keys = [f"k{i}" for i in range(len(plan.keys))]
        # The same row per duplicated key as the SQL engines: smallest hash, then tolerance values.
        order = [c for c in s.columns if c == "h" or c[0] == "n"]

        def prepare(frame: Any) -> tuple[Any, Any]:
            not_null = frame.filter(pl.all_horizontal([pl.col(k).is_not_null() for k in keys]))
            unique = not_null.sort(order, nulls_last=True).unique(
                subset=keys, keep="first", maintain_order=True
            )
            return not_null, unique

        s_nn, s1 = prepare(s)
        t_nn, t1 = prepare(t)
        joined = s1.rename({c: f"s_{c}" for c in s1.columns}).join(
            t1.rename({c: f"t_{c}" for c in t1.columns}),
            left_on=[f"s_{k}" for k in keys],
            right_on=[f"t_{k}" for k in keys],
            how="full",
            coalesce=False,
        )
        in_s = pl.col(f"s_{keys[0]}").is_not_null()
        in_t = pl.col(f"t_{keys[0]}").is_not_null()
        flags: dict[int, Any] = {}
        for j, col in enumerate(plan.columns):
            numeric = col.pair_spec.numeric
            if numeric is not None:
                a, b = pl.col(f"s_n{j}"), pl.col(f"t_n{j}")
                allowed = pl.max_horizontal(
                    pl.lit(float(numeric.abs_tol)),
                    pl.lit(float(numeric.rel_tol)) * pl.max_horizontal(a.abs(), b.abs()),
                )
                flags[j] = (
                    pl.when(a.is_null() & b.is_null())
                    .then(False)
                    .when(a.is_null() | b.is_null())
                    .then(True)
                    .otherwise((a - b).abs() > allowed)
                )
            elif job.mode == "full":
                flags[j] = pl.col(f"s_c{j}").ne_missing(pl.col(f"t_c{j}"))
        row_parts = [f for j, f in flags.items() if plan.columns[j].pair_spec.numeric is not None]
        row_parts.append(pl.col("s_h").ne_missing(pl.col("t_h")))
        row_flag = pl.any_horizontal(row_parts) if row_parts else pl.lit(False)
        joined = joined.with_columns(
            in_s.alias("in_s"),
            in_t.alias("in_t"),
            row_flag.alias("mrow"),
            *[f.alias(f"m{j}") for j, f in flags.items()],
        )
        both = pl.col("in_s") & pl.col("in_t")
        counts = joined.select(
            (pl.col("in_s") & ~pl.col("in_t")).sum().alias("missing"),
            (~pl.col("in_s") & pl.col("in_t")).sum().alias("extra"),
            (both & pl.col("mrow")).sum().alias("mismatched"),
            (both & ~pl.col("mrow")).sum().alias("matched"),
            *[(both & pl.col(f"m{j}")).sum().alias(f"m{j}") for j in flags],
        ).row(0, named=True)
        summary = ReconciliationSummary(
            matched=int(counts["matched"]),
            mismatched=int(counts["mismatched"]),
            missing_in_target=int(counts["missing"]),
            extra_in_target=int(counts["extra"]),
            duplicate_keys_source=s_nn.height - s1.height,
            duplicate_keys_target=t_nn.height - t1.height,
            null_keys_source=s.height - s_nn.height,
            null_keys_target=t.height - t_nn.height,
        )
        result = DiffResult(
            engine=self.name, source_rows=s.height, target_rows=t.height, summary=summary
        )
        for j in flags:
            result.column_counts[plan.columns[j].label] = int(counts[f"m{j}"])
        s_keys = [f"s_{k}" for k in keys]
        if job.mode == "full":
            flagged = [j for j in flags if result.column_counts[plan.columns[j].label]]
            per_column = max(1, job.max_mismatch_rows // max(1, len(flagged)))
            for j in flagged:
                col = plan.columns[j]
                canon = (
                    (f"s_c{j}", f"t_c{j}")
                    if col.pair_spec.numeric is None
                    else (f"s_n{j}", f"t_n{j}")
                )
                rows = (
                    joined.filter(both & pl.col(f"m{j}"))
                    .sort(s_keys)
                    .head(per_column)
                    .select([f"s_rk{i}" for i in range(len(keys))] + [f"s_r{j}", f"t_r{j}", *canon])
                    .rows()
                )
                for row in rows:
                    n = len(keys)
                    result.mismatches.append(
                        MismatchRow(
                            keys=list(row[:n]),
                            column=col.label,
                            source_value=row[n],
                            target_value=row[n + 1],
                            source_canonical=None if row[n + 2] is None else str(row[n + 2]),
                            target_canonical=None if row[n + 3] is None else str(row[n + 3]),
                        )
                    )
        if job.sample_size > 0:
            self._key_samples(result, joined, s, t, s_nn, t_nn, keys, job.sample_size)
        result.timing.source_query_ms = source_ms
        result.timing.target_query_ms = target_ms
        result.timing.compare_ms = _ms(started)
        return result

    def _key_samples(
        self,
        result: DiffResult,
        joined: pl.DataFrame,
        s: pl.DataFrame,
        t: pl.DataFrame,
        s_nn: pl.DataFrame,
        t_nn: pl.DataFrame,
        keys: list[str],
        limit: int,
    ) -> None:
        pl = _import_polars()
        n = len(keys)
        raw = [f"rk{i}" for i in range(n)]

        def add(kind: str, frame: Any, columns: list[str], with_count: str | None = None) -> None:
            if frame.height == 0:
                return
            selected = frame.head(limit).select(columns + ([with_count] if with_count else []))
            rows = [list(r) + ([] if with_count else [1]) for r in selected.rows()]
            result.key_samples[kind] = rows

        add(
            "missing_in_target",
            joined.filter(pl.col("in_s") & ~pl.col("in_t")).sort([f"s_{k}" for k in keys]),
            [f"s_{c}" for c in raw],
        )
        add(
            "extra_in_target",
            joined.filter(~pl.col("in_s") & pl.col("in_t")).sort([f"t_{k}" for k in keys]),
            [f"t_{c}" for c in raw],
        )
        for kind, frame in (("duplicate_in_source", s_nn), ("duplicate_in_target", t_nn)):
            dups = (
                frame.group_by(keys)
                .agg([pl.col(c).min() for c in raw] + [pl.len().alias("dvf_count")])
                .filter(pl.col("dvf_count") > 1)
                .sort(keys)
            )
            add(kind, dups, raw, "dvf_count")
        for kind, frame in (("null_key_in_source", s), ("null_key_in_target", t)):
            nulls = frame.filter(pl.any_horizontal([pl.col(k).is_null() for k in keys]))
            add(kind, nulls, raw)
