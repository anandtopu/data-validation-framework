"""A rich, fully synthetic ``RunResult`` for reporter tests. No real data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dvf.core.datasets import DatasetKind, DatasetRef
from dvf.core.enums import Dimension, Severity, Side, Status
from dvf.core.types import CanonicalType, Compatibility
from dvf.models.results import (
    ArtifactRef,
    BaselineComparison,
    CheckResult,
    ColumnMismatch,
    ColumnProfile,
    ColumnSchemaDiff,
    DatasetPairResult,
    ErrorInfo,
    MetricComparison,
    ProfileComparison,
    QueryRecord,
    ReconciliationSummary,
    RowCounts,
    RunEnvironment,
    RunResult,
    RunSummary,
    SampleSet,
    SuiteResult,
    TimingBreakdown,
    TrendPoint,
    WaiverApplied,
    WaiverStatus,
    aggregate_status,
)

XSS = "<script>alert(1)</script>"
WAIVER_XSS = "<img src=x onerror=alert(3)>"
PIPE_MESSAGE = "38 rows differ | see side-by-side samples"
STARTED = datetime(2026, 11, 6, 9, 30, tzinfo=UTC)
BASELINE_RUN_ID = "20261105T093000Z-prev01"
PARTITIONS = [
    {
        "name": "region=EU",
        "status": "failed",
        "source_rows": 6000,
        "target_rows": 5988,
        "mismatched": 38,
        "missing_in_target": 12,
        "extra_in_target": 0,
    },
    {
        "name": "region=US",
        "status": "passed",
        "source_rows": 4000,
        "target_rows": 4000,
        "mismatched": 0,
        "missing_in_target": 0,
        "extra_in_target": 0,
    },
]


def _ref(connection: str, name: str) -> DatasetRef:
    return DatasetRef(connection=connection, kind=DatasetKind.TABLE, name=name)


def _check(
    check_id: str,
    status: Status,
    *,
    check_type: str,
    dimension: Dimension = Dimension.VALIDITY,
    severity: Severity = Severity.ERROR,
    duration_ms: float = 12.5,
    **fields: object,
) -> CheckResult:
    data: dict[str, object] = {
        "check_id": check_id,
        "name": check_id.rsplit(".", 1)[-1],
        "type": check_type,
        "dimension": dimension,
        "severity": severity,
        "status": status,
        "duration_ms": duration_ms,
        **fields,
    }
    return CheckResult.model_validate(data)


def _customers() -> DatasetPairResult:
    compare_samples = SampleSet(
        columns=["customer_id", "source.email", "target.email"],
        rows=[
            ["C001", "ann@example.test ", "ann@example.test"],
            ["C002", "BOB@EXAMPLE.TEST", "bob@example.test"],
            ["C003", XSS, "a|b"],
            ["C004", None, ""],
        ],
        masked=False,
        truncated=True,
    )
    column_compare = _check(
        "sales_migration.customers.column_compare",
        Status.FAILED,
        check_type="column_compare",
        dimension=Dimension.RECONCILIATION,
        severity=Severity.CRITICAL,
        side=Side.BOTH,
        tags=["migration", "pii"],
        owner="data-qa@example.test",
        columns=["email", "full_name", "balance", "created_at"],
        message=PIPE_MESSAGE,
        observed={"mismatched_rows": 38},
        expected={"mismatched_rows": 0},
        unexpected_count=38,
        total_count=9988,
        unexpected_pct=0.380457,
        mostly=1.0,
        samples=compare_samples,
        column_mismatches=[
            ColumnMismatch(
                column="email",
                source_column="EMAIL_ADDR",
                mismatch_count=15,
                mismatch_pct=0.150180,
                classifications={"whitespace_only": 12, "case_only": 3},
                hints=["80% of mismatches are trailing spaces; consider 'trim: rtrim'."],
                samples=compare_samples,
            ),
            ColumnMismatch(
                column="full_name",
                mismatch_count=5,
                mismatch_pct=0.05,
                classifications={"case_only": 5},
                hints=["Names differ only in case; consider 'case: lower'."],
                waived=True,
            ),
            ColumnMismatch(
                column="balance",
                mismatch_count=8,
                mismatch_pct=0.08,
                classifications={"numeric_rounding": 8},
                hints=["Differences are within 2 decimals; check DECIMAL scale."],
                samples=SampleSet(
                    columns=["customer_id", "source.balance", "target.balance"],
                    rows=[["C010", 10.005, 10.01], ["C011", 3.14159, 3.14]],
                ),
            ),
            ColumnMismatch(
                column="created_at",
                mismatch_count=10,
                mismatch_pct=0.1,
                classifications={"timezone_shift": 10},
                hints=["All differences are exactly 2 hours; check time zone handling."],
            ),
        ],
        hints=["Fix the CHAR to VARCHAR mapping for EMAIL_ADDR."],
        artifacts=[
            ArtifactRef(
                kind="extract",
                path="extracts/customers.mismatches.parquet",
                format="parquet",
                rows=38,
            ),
            ArtifactRef(kind="repro", path="javascript:alert(2)", format="sql"),
            ArtifactRef(
                kind="repro_sql",
                path="repro/sales_migration.customers.column_compare.source.sql",
                format="sql",
            ),
            ArtifactRef(
                kind="repro_config",
                path="repro/sales_migration.customers.column_compare.yaml",
                format="yaml",
            ),
        ],
        queries=[
            QueryRecord(
                side=Side.SOURCE,
                dialect="tsql",
                sql="SELECT customer_id, RTRIM(email_addr) AS email\nFROM crm.customers\nWHERE 1 < 2",
                parameters={"batch": 1},
                engine="pushdown",
                duration_ms=410.0,
                rows_returned=10000,
            ),
            QueryRecord(
                side=Side.TARGET,
                dialect="postgres",
                sql="SELECT customer_id, email FROM dw.dim_customer",
                engine="pushdown",
                duration_ms=380.0,
                rows_returned=9988,
            ),
        ],
        normalization=["trim: rtrim", "case: lower", "timezone: UTC"],
        engine="duckdb",
        partition="region=EU",
        started_at=STARTED,
        ended_at=STARTED + timedelta(seconds=2),
        duration_ms=2000.0,
        timing=TimingBreakdown(
            source_query_ms=410.0,
            target_query_ms=380.0,
            transfer_ms=600.0,
            compare_ms=550.0,
            render_ms=60.0,
            total_ms=2000.0,
        ),
        details={
            "comparison_mode": "row_hash",
            "buckets": [1, 2, 3],
            "ticket": "DQ-42",
            "partitions": PARTITIONS,
        },
        metadata={"release": "2026.11", "nested": {"approved": True}},
    )
    row_count = _check(
        "sales_migration.customers.row_count",
        Status.FAILED,
        check_type="row_count",
        dimension=Dimension.VOLUME,
        message="Row counts differ in 1 of 2 regions.",
        observed=9988,
        expected=10000,
        waiver=WaiverApplied(
            selector="sales_migration.customers.row_count",
            reason="Recent EU rows load in the next wave",
            ticket="MIG-77",
            expires="2026-10-31",
            approved_by="architect",
            expired=True,
        ),
        metrics=[
            MetricComparison(
                metric="row_count",
                group={"region": "EU"},
                source=6000,
                target=5988,
                difference=-12,
                tolerance=0,
                passed=False,
                note="missing recent rows",
            ),
            MetricComparison(
                metric="row_count",
                group={"region": "US"},
                source=4000,
                target=4000,
                difference=0,
                tolerance=0,
                passed=True,
            ),
            MetricComparison(
                metric="sum",
                column="balance",
                source_column="BALANCE",
                source=1234.5,
                target=1234.5,
                difference=0.0,
                tolerance=0.01,
                passed=True,
            ),
        ],
    )
    not_null = _check(
        "sales_migration.customers.not_null.email",
        Status.PASSED,
        check_type="not_null",
        dimension=Dimension.COMPLETENESS,
        columns=["email"],
        unexpected_count=0,
        total_count=9988,
        unexpected_pct=0.0,
    )
    unique = _check(
        "sales_migration.customers.unique.customer_id",
        Status.WARNING,
        check_type="unique",
        dimension=Dimension.UNIQUENESS,
        severity=Severity.WARNING,
        columns=["customer_id"],
        message="1 duplicate key in source.",
        unexpected_count=1,
        total_count=10000,
        unexpected_pct=0.01,
    )
    schema = _check(
        "sales_migration.customers.type_compatibility",
        Status.FAILED,
        check_type="type_compatibility",
        dimension=Dimension.SCHEMA,
        message="2 columns are not type compatible.",
    )
    return DatasetPairResult(
        name="customers",
        source=_ref("legacy", "crm.customers"),
        target=_ref("warehouse", "dw.dim_customer"),
        status=aggregate_status(
            c.status for c in (column_compare, row_count, not_null, unique, schema)
        ),
        row_counts=RowCounts(source=10000, target=9988),
        reconciliation=ReconciliationSummary(
            matched=9950,
            mismatched=38,
            missing_in_target=12,
            extra_in_target=0,
            duplicate_keys_source=1,
            null_keys_target=2,
        ),
        schema_diff=[
            ColumnSchemaDiff(
                source_column="CUSTOMER_ID",
                target_column="customer_id",
                status="matched",
                source_type=CanonicalType.integer(32),
                target_type=CanonicalType.integer(64),
                source_native_type="INT",
                target_native_type="BIGINT",
                compatibility=Compatibility.WIDENING,
            ),
            ColumnSchemaDiff(
                source_column="EMAIL_ADDR",
                target_column="email",
                status="matched",
                source_type=CanonicalType.string(100, fixed=True),
                target_type=CanonicalType.string(100),
                source_native_type="CHAR(100)",
                target_native_type="VARCHAR(100)",
                compatibility=Compatibility.EXACT,
            ),
            ColumnSchemaDiff(
                source_column="BALANCE",
                target_column="balance",
                status="matched",
                source_type=CanonicalType.decimal(18, 4),
                target_type=CanonicalType.decimal(18, 2),
                source_native_type="DECIMAL(18,4)",
                target_native_type="NUMERIC(18,2)",
                compatibility=Compatibility.LOSSY,
                reasons=["scale 4 -> 2 loses digits"],
                nullable_change="NOT NULL -> NULL",
            ),
            ColumnSchemaDiff(
                source_column="CREATED_AT",
                target_column="created_at",
                status="matched",
                source_type=CanonicalType.timestamp(3),
                target_type=CanonicalType.string(),
                source_native_type="DATETIME2(3)",
                target_native_type="TEXT",
                compatibility=Compatibility.INCOMPATIBLE,
                reasons=["TIMESTAMP stored as STRING"],
            ),
            ColumnSchemaDiff(
                source_column="FAX",
                target_column=None,
                status="missing_in_target",
                source_native_type="VARCHAR(20)",
            ),
        ],
        profiles=[
            ProfileComparison(
                column="email",
                source_column="EMAIL_ADDR",
                source=ColumnProfile(
                    count=10000,
                    null_count=4,
                    null_pct=0.04,
                    distinct_count=9990,
                    min_length=5,
                    max_length=100,
                    avg_length=24.5,
                ),
                target=ColumnProfile(
                    count=9988,
                    null_count=0,
                    null_pct=0.0,
                    distinct_count=9984,
                    min_length=5,
                    max_length=64,
                    avg_length=21.2,
                ),
                differences=["null_count 4 -> 0", "max_length 100 -> 64"],
            ),
            ProfileComparison(
                column="balance",
                source=ColumnProfile(count=10000, null_count=0, min=0, max=99999.5, mean=512.25),
                target=ColumnProfile(count=9988, null_count=0, min=0, max=99999.5, mean=512.25),
            ),
        ],
        checks=[column_compare, row_count, not_null, unique, schema],
        duration_ms=2600.0,
        details={"keys": ["customer_id"], "strategy": "full_diff"},
    )


def _orders() -> DatasetPairResult:
    errored = _check(
        "sales_migration.orders.aggregate_compare",
        Status.ERROR,
        check_type="aggregate_compare",
        dimension=Dimension.ACCURACY,
        message="Target query failed.",
        error=ErrorInfo(
            type="OperationalError",
            message='relation "dw.fact_orders" does not exist',
            traceback='Traceback (most recent call last):\n  File "engine.py", line 1\nOperationalError: relation missing',
            hint="Check the target table name or run the migration first.",
        ),
        duration_ms=150.0,
    )
    passed = _check(
        "sales_migration.orders.not_null.order_id",
        Status.PASSED,
        check_type="not_null",
        dimension=Dimension.COMPLETENESS,
        columns=["order_id"],
    )
    waived = _check(
        "sales_migration.orders.accepted_values.status",
        Status.WAIVED,
        check_type="accepted_values",
        message="2 unexpected status codes.",
        unexpected_count=2,
        waiver=WaiverApplied(
            selector="sales_migration.orders.accepted_values.*",
            reason="Legacy codes retired after cut-over",
            ticket="MIG-101",
            expires="2026-12-31",
            approved_by="qa.lead",
        ),
    )
    return DatasetPairResult(
        name="orders",
        source=_ref("legacy", "sales.orders"),
        target=_ref("warehouse", "dw.fact_orders"),
        status=Status.ERROR,
        row_counts=RowCounts(source=50000),
        checks=[errored, passed, waived],
        duration_ms=300.0,
    )


def _reference_suite() -> SuiteResult:
    countries = DatasetPairResult(
        name="countries",
        status=Status.PASSED,
        row_counts=RowCounts(source=250, target=250),
        reconciliation=ReconciliationSummary(matched=250),
        checks=[
            _check(
                "reference_data.countries.row_count",
                Status.PASSED,
                check_type="row_count",
                dimension=Dimension.VOLUME,
                observed=250,
                expected=250,
            ),
            _check(
                "reference_data.countries.freshness",
                Status.SKIPPED,
                check_type="freshness",
                dimension=Dimension.TIMELINESS,
                message="No timestamp column configured.",
            ),
        ],
        duration_ms=40.0,
    )
    currencies = DatasetPairResult(
        name="currencies",
        status=Status.SKIPPED,
        checks=[
            _check(
                "reference_data.currencies.row_count",
                Status.SKIPPED,
                check_type="row_count",
                dimension=Dimension.VOLUME,
            )
        ],
    )
    return SuiteResult(
        name="reference_data",
        status=Status.PASSED,
        tags=["reference"],
        datasets=[countries, currencies],
        duration_ms=60.0,
    )


def build_run() -> RunResult:
    """Two suites, four tables, and checks of every status."""
    sales = SuiteResult(
        name="sales_migration",
        status=Status.ERROR,
        tags=["migration"],
        datasets=[_customers(), _orders()],
        duration_ms=2900.0,
    )
    run = RunResult(
        run_id="20261106T093000Z-abc123",
        project="synthetic-migration",
        status=Status.ERROR,
        started_at=STARTED,
        ended_at=STARTED + timedelta(seconds=3),
        duration_ms=3000.0,
        environment=RunEnvironment(
            dvf_version="0.1.0",
            python_version="3.12.7",
            platform="Linux-6.8-x86_64",
            hostname="ci-runner-7",
            profile="ci",
            git_sha="0123456789abcdef0123456789abcdef01234567",
            config_files=["dvf.yaml", "dvf.ci.yaml"],
            config_hash="sha256:feedface",
            cli_args=["run", "--profile", "ci", "--set", "reports.include_passed=true"],
            plugins={"dvf-acme": "1.2.0"},
            engines={"duckdb": "1.4.1", "sqlglot": "30.0.1"},
        ),
        summary=RunSummary(rows_compared=60238),
        suites=[sales, _reference_suite()],
        config_snapshot={
            "version": 1,
            "project": {"name": "synthetic-migration"},
            "connections": {"legacy": {"type": "mssql", "password": "***"}},
        },
        metadata={"release": "R-2026.11", "approver": "QA Lead"},
        waivers=_waivers(),
        baseline=BaselineComparison(
            baseline_run_id=BASELINE_RUN_ID,
            baseline_started_at=STARTED - timedelta(days=1),
            policy="last_run",
            new_failures=[
                "sales_migration.customers.column_compare",
                "sales_migration.orders.aggregate_compare",
            ],
            resolved=["sales_migration.customers.not_null.email"],
            persisting=[
                "sales_migration.customers.row_count",
                "sales_migration.customers.type_compatibility",
            ],
        ),
        trend=[
            TrendPoint(
                run_id="20261103T093000Z-aaa111",
                started_at=STARTED - timedelta(days=3),
                status=Status.FAILED,
                quality_score=80.0,
                checks=11,
                failed=2,
            ),
            TrendPoint(
                run_id="20261104T093000Z-bbb222",
                started_at=STARTED - timedelta(days=2),
                status=Status.FAILED,
                quality_score=None,
                checks=11,
                failed=3,
            ),
            TrendPoint(
                run_id=BASELINE_RUN_ID,
                started_at=STARTED - timedelta(days=1),
                status=Status.ERROR,
                quality_score=62.5,
                checks=11,
                failed=3,
                errors=1,
            ),
        ],
    )
    return run.model_copy(update={"summary": run.summarize()})


def _waivers() -> list[WaiverStatus]:
    return [
        WaiverStatus(
            selector="sales_migration.orders.accepted_values.*",
            reason="Legacy codes retired after cut-over",
            ticket="MIG-101",
            approved_by="qa.lead",
            expires="2026-12-31",
            status="applied",
            checks=["sales_migration.orders.accepted_values.status"],
        ),
        WaiverStatus(
            selector="sales_migration.customers.column_compare",
            column="full_name",
            reason="Case differences in names are accepted",
            ticket="MIG-55",
            status="applied",
            checks=["sales_migration.customers.column_compare"],
        ),
        WaiverStatus(
            selector="sales_migration.customers.row_count",
            reason="Recent EU rows load in the next wave",
            ticket="MIG-77",
            approved_by="architect",
            expires="2026-10-31",
            status="expired",
            checks=["sales_migration.customers.row_count"],
        ),
        WaiverStatus(
            selector="reference_data.*.freshness",
            reason=f"Not tracked {WAIVER_XSS}",
            status="unused",
        ),
    ]


def many_checks_run(count: int, status: Status = Status.FAILED) -> RunResult:
    """A run with ``count`` similar checks in one table, for scale and truncation tests."""
    checks = [
        _check(
            f"bulk.big_table.not_null.col_{n:05d}",
            status,
            check_type="not_null",
            dimension=Dimension.COMPLETENESS,
            columns=[f"col_{n:05d}"],
            message=f"Column col_{n:05d} has {n} null values | threshold 0",
            unexpected_count=n,
            total_count=1_000_000,
            unexpected_pct=n / 10_000,
        )
        for n in range(1, count + 1)
    ]
    dataset = DatasetPairResult(name="big_table", status=status, checks=checks)
    return RunResult(
        run_id="bulk-run",
        project="bulk",
        status=status,
        started_at=STARTED,
        duration_ms=1000.0,
        environment=RunEnvironment(dvf_version="0.1.0", python_version="3.12", platform="x"),
        suites=[SuiteResult(name="bulk", status=status, datasets=[dataset])],
    )
