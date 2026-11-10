"""Files written next to the reports for failed checks (BUILD_PROMPT.md section 9.3).

* **Extracts** (``extracts/<check>.parquet`` or ``.csv``): mismatched values, key issues, and
  failing rows beyond the samples kept in the results, masked like samples. Strict privacy mode
  writes none.
* **Repro files** (``repro/<check>/``): the exact SQL that ran on each side, and ``check.yaml``, a
  configuration that extends the original and runs only this check (``dvf run repro/.../check.yaml``).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq
import yaml

from dvf.config.checks import CheckConfig
from dvf.config.models import SuiteConfig, TableConfig
from dvf.core.enums import Side
from dvf.models.results import ArtifactRef, CheckResult
from dvf.security.masking import to_json_safe

_UNSAFE = re.compile(r"[^A-Za-z0-9_.+=-]+")


def safe_name(check_id: str) -> str:
    """A file or folder name for ``check_id`` that cannot leave its folder (no separators, no
    leading dots, so never ``.`` or ``..``)."""
    name = _UNSAFE.sub("_", check_id)[:150]
    return "_" + name.lstrip(".") if not name or name.startswith(".") else name


def text_table(columns: list[str], rows: list[list[Any]]) -> pa.Table:
    """An all-text Arrow table (masked values are text, so every column is text)."""
    data = {
        name: [None if r[i] is None else str(to_json_safe(r[i])) for r in rows]
        for i, name in enumerate(columns)
    }
    return pa.table({name: pa.array(values, pa.string()) for name, values in data.items()})


def write_extract(
    output_dir: Path, check_id: str, table: pa.Table, fmt: str, max_rows: int
) -> ArtifactRef:
    folder = output_dir / "extracts"
    folder.mkdir(parents=True, exist_ok=True)
    table = table.slice(0, max_rows)
    path = folder / f"{safe_name(check_id)}.{fmt}"
    if fmt == "csv":
        pa_csv.write_csv(table, path)
    else:
        pq.write_table(table, path)
    return ArtifactRef(
        kind="extract",
        path=path.relative_to(output_dir).as_posix(),
        format=fmt,
        rows=table.num_rows,
    )


def _sql_file(result: CheckResult, side: Side) -> str | None:
    queries = [q for q in result.queries if q.side is side]
    if not queries:
        return None
    parts = [
        f"-- {result.check_id}: query {i} of {len(queries)} on the {side.value} side "
        f"({q.dialect or 'unknown dialect'}, {q.duration_ms or 0:.0f} ms, "
        f"{q.rows_returned if q.rows_returned is not None else '?'} rows)\n{q.sql.rstrip(';')};\n"
        for i, q in enumerate(queries, start=1)
    ]
    return "\n".join(parts)


def write_repro(
    output_dir: Path,
    result: CheckResult,
    *,
    run_id: str,
    config_file: Path | None,
    profile: str | None,
    suite: SuiteConfig,
    table: TableConfig,
    check: CheckConfig | None,
) -> list[ArtifactRef]:
    folder = output_dir / "repro" / safe_name(result.check_id)
    folder.mkdir(parents=True, exist_ok=True)
    artifacts: list[ArtifactRef] = []
    for side in (Side.SOURCE, Side.TARGET, Side.BOTH):
        text = _sql_file(result, side)
        if text is None:
            continue
        path = folder / f"{side.value}.sql"
        path.write_text(text, encoding="utf-8")
        artifacts.append(
            ArtifactRef(
                kind="repro_sql", path=path.relative_to(output_dir).as_posix(), format="sql"
            )
        )
    if config_file is not None:
        table_data = table.model_dump(mode="json", by_alias=True, exclude_none=True)
        if check is not None:
            table_data["checks"] = [check.model_dump(mode="json", by_alias=True, exclude_none=True)]
            table_data["comparison"] = {**table_data.get("comparison", {}), "enabled": False}
            table_data["schema_checks"] = []
        else:
            table_data["checks"] = []
            if result.type.startswith("schema."):
                table_data["comparison"] = {**table_data.get("comparison", {}), "enabled": False}
        suite_data = suite.model_dump(mode="json", by_alias=True, exclude_none=True)
        suite_data["tables"] = [table_data]
        try:
            extends = Path(os.path.relpath(config_file, folder)).as_posix()
        except ValueError:  # another drive on Windows
            extends = config_file.as_posix()
        profile_flag = f" --profile {profile}" if profile else ""
        header = (
            f"# Reproduces {result.check_id} from run {run_id}.\n"
            f"# Run it with: dvf run repro/{folder.name}/check.yaml{profile_flag}\n"
            "# (from the report folder).\n"
            "# It extends the original configuration, so connections and secrets resolve as before.\n"
        )
        body = yaml.safe_dump(
            {"extends": [extends], "suites": [suite_data]}, sort_keys=False, allow_unicode=True
        )
        path = folder / "check.yaml"
        path.write_text(header + body, encoding="utf-8")
        artifacts.append(
            ArtifactRef(
                kind="repro_config", path=path.relative_to(output_dir).as_posix(), format="yaml"
            )
        )
    return artifacts
